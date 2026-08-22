#!/usr/bin/env python3

import csv
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from bs4 import BeautifulSoup
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

# ============================================================
# Configuration
# ============================================================

ROOT = Path(__file__).resolve().parents[2]

SOURCE_DIR = ROOT / ".github" / "source"
OUTPUT_DIR = ROOT / "data"

BATCHES = {
    # "2023_2027": {
    #     "csv": SOURCE_DIR / "2023_2027.csv",
    #     "markdown": ROOT / "2023 - 2027.md",
    # },
    "2024_2028": {
        "csv": SOURCE_DIR / "2024_2028.csv",
        "markdown": ROOT / "2024-2028.md",
    },
}

PROFILE_URL = "https://tryhackme.com/p/{username}"

USER_AGENT = (
    "Mozilla/5.0 "
    "(X11; Linux x86_64) "
    "AppleWebKit/537.36 "
    "(KHTML, like Gecko) "
    "Chrome/139.0 Safari/537.36"
)

NAV_TIMEOUT_MS = 30000
IDLE_SOFT_WAIT_MS = 8000
POST_LOAD_WAIT_MS = 3500
REQUEST_DELAY = 2

# Set THM_DEBUG=1 in the environment to print diagnostic
# output (captured JSON, rendered text, and exactly which
# key/pattern produced the room count) for every profile.
DEBUG = os.environ.get("THM_DEBUG") == "1"

# ============================================================
# CSV handling
# ============================================================

def load_students(csv_file):
    """
    Load:
        register_number,tryhackme_username
    from a batch CSV.
    """

    students = {}

    if not csv_file.exists():
        raise FileNotFoundError(
            f"CSV file not found: {csv_file}"
        )

    with csv_file.open(
        "r",
        encoding="utf-8-sig",
        newline=""
    ) as file:
        reader = csv.DictReader(file)
        required = {
            "register_number",
            "tryhackme_username",
        }
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(
                f"{csv_file} must contain columns: "
                "register_number, tryhackme_username"
            )
        for row in reader:
            register_number = row[
                "register_number"
            ].strip()
            username = row[
                "tryhackme_username"
            ].strip()
            if not register_number:
                continue
            if not username:
                students[register_number] = {
                    "username": None,
                    "rooms": None,
                    "status": "no_username",
                }
                continue
            students[register_number] = {
                "username": username,
                "rooms": None,
                "status": "pending",
            }
    return students

# ============================================================
# TryHackMe (rendered fetch)
# ============================================================

def fetch_profile(username, page):
    """
    Load the public TryHackMe profile in a real browser context
    so client-side rendered data (rooms completed, badges, etc.)
    is actually present, then return:
        html_content, rendered_text, captured_json, status
    No authentication or private endpoints are used.
    """

    url = PROFILE_URL.format(username=username)

    captured_json = []

    def handle_response(response):
        try:
            content_type = response.headers.get("content-type", "")
            if "application/json" not in content_type:
                return
            body = response.json()
            captured_json.append(body)
        except Exception:
            # Response body may not be JSON-parseable, or the
            # response may have already been consumed/closed.
            pass

    page.on("response", handle_response)

    try:
        # Only wait for the initial document + DOM to be ready.
        # Do NOT wait for "networkidle" here: TryHackMe keeps
        # background connections open (analytics, sockets,
        # polling) that mean network idle may never occur,
        # which previously caused a hard timeout.
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=NAV_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError:
        page.remove_listener("response", handle_response)
        return None, None, [], "timeout"
    except Exception as exc:
        page.remove_listener("response", handle_response)
        return None, None, [], f"request_error:{type(exc).__name__}"

    if response is None:
        page.remove_listener("response", handle_response)
        return None, None, [], "no_response"
    if response.status == 404:
        page.remove_listener("response", handle_response)
        return None, None, [], "profile_not_found"
    if response.status == 429:
        page.remove_listener("response", handle_response)
        return None, None, [], "rate_limited"
    if response.status >= 400:
        page.remove_listener("response", handle_response)
        return None, None, [], f"http_{response.status}"

    # Soft-wait for network idle: give the page a bounded
    # window to settle its initial data fetches, but don't
    # fail the whole run if it never fully idles.
    try:
        page.wait_for_load_state(
            "networkidle",
            timeout=IDLE_SOFT_WAIT_MS,
        )
    except PlaywrightTimeoutError:
        pass

    # Extra fixed buffer for React to hydrate and paint stats
    # even if some background network activity is still going.
    # Keep the response listener attached through this window
    # in case the stats fetch lands late.
    page.wait_for_timeout(POST_LOAD_WAIT_MS)

    page.remove_listener("response", handle_response)

    html = page.content()
    try:
        visible_text = page.inner_text("body")
    except Exception:
        visible_text = ""

    return html, visible_text, captured_json, "ok"

# ============================================================
# Room count extraction
# ============================================================

def _walk_json(node, path, predicate):
    """
    Recursively walk a JSON structure, yielding (value, key_path)
    for every scalar entry whose key satisfies `predicate`.
    """

    if isinstance(node, dict):
        for key, value in node.items():
            key_path = f"{path}.{key}"
            if isinstance(value, (int, float)) and predicate(key.lower()):
                yield int(value), key_path
            yield from _walk_json(value, key_path, predicate)

    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from _walk_json(item, f"{path}[{index}]", predicate)


def _search_json_for_room_count(node):
    """
    Search a captured JSON payload for a key that looks like a
    "rooms completed" count.

    Returns (value, key_path) so callers can verify/debug
    exactly which field was matched, rather than trusting a
    bare number blindly. Returns None if nothing matches.

    Strict matches (key contains both "room" and "complet") are
    preferred over loose matches (key contains "room" and ends
    with "count") because loose keys are more likely to refer to
    something else, e.g. "roomsInProgressCount".
    """

    strict = list(
        _walk_json(
            node,
            "$",
            lambda k: "room" in k and "complet" in k,
        )
    )
    if strict:
        return strict[0]

    loose = list(
        _walk_json(
            node,
            "$",
            lambda k: "room" in k and k.endswith("count") and "complet" not in k,
        )
    )
    if loose:
        return loose[0]

    return None


def _class_contains(tag, substring):
    """
    True if `tag` has a class attribute containing `substring`
    in any of its class tokens. Styled-components appends a
    build-specific hash suffix (e.g. "...-sc-3b126e6-30") that
    changes between deployments, so we match on the stable
    semantic prefix instead of the full class name.
    """

    classes = tag.get("class") or []
    return any(substring in cls for cls in classes)


def _extract_from_stat_boxes(soup):
    """
    TryHackMe's public profile renders each stat (Completed
    rooms, Badges, etc.) as a label element followed by a
    sibling number element, e.g.:

        <div class="...StyledStatisticsBoxText...">
            Completed rooms
        </div>
        <div class="...StyledStatisticsBoxIconNumberContainer...">
            <div class="...StyledStatisticsBoxIcon...">...</div>
            <span class="...StyledStatisticsBoxNumber...">40</span>
        </div>

    This walks the DOM directly for that structure rather than
    relying on text proximity, since the label and number are
    not adjacent in the same text run.
    """

    labels = soup.find_all(
        lambda tag: tag.name in ("div", "span")
        and _class_contains(tag, "StatisticsBoxText")
    )

    for label in labels:
        label_text = label.get_text(strip=True).lower()
        if "room" not in label_text:
            continue
        if "complet" not in label_text:
            continue

        number_tag = label.find_next(
            lambda tag: tag.name in ("span", "div")
            and _class_contains(tag, "StatisticsBoxNumber")
        )

        if number_tag is None:
            continue

        number_text = number_tag.get_text(strip=True).replace(",", "")

        if number_text.isdigit():
            source = (
                f"DOM stat box: label={label_text!r} "
                f"number_tag_class={number_tag.get('class')}"
            )
            return int(number_text), source

    return None, None


def extract_room_count(html, visible_text, captured_json):
    """
    Extract a room-completion count from the rendered profile.
    Tries, in order:
        1. The "Completed rooms" stat box in the DOM (most
           reliable — matches the site's actual markup).
        2. Any JSON responses captured while the page loaded.
        3. Visible rendered text on the page.
        4. Raw HTML as a last resort (in case data is inlined
           in a <script> tag rather than fetched separately).
    Returns:
        (rooms, source_description) where rooms is an int or
        None if no room-completion count is found. source_description
        explains exactly where the value came from, for debugging.
    """

    # --------------------------------------------------------
    # 1. DOM stat box (label + sibling number element).
    # --------------------------------------------------------

    soup = BeautifulSoup(html or "", "html.parser")
    dom_result, dom_source = _extract_from_stat_boxes(soup)
    if dom_result is not None:
        return dom_result, dom_source

    # --------------------------------------------------------
    # 2. Captured JSON responses.
    # --------------------------------------------------------

    for blob_index, blob in enumerate(captured_json):
        result = _search_json_for_room_count(blob)
        if result is not None:
            value, key_path = result
            return value, f"json[{blob_index}] key {key_path}"

    # --------------------------------------------------------
    # 3. Rendered visible text.
    # --------------------------------------------------------

    patterns = [
        r"\b(\d[\d,]*)\s+Rooms?\s+Completed\b",
        r"\bRooms?\s+Completed\s*:?\s*(\d[\d,]*)\b",
        r"\b(\d[\d,]*)\s+rooms?\s+completed\b",
        r"\brooms?\s+completed\s*:?\s*(\d[\d,]*)\b",
        r"Completed\s+rooms?\D{0,20}?(\d[\d,]*)",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            visible_text or "",
            flags=re.IGNORECASE,
        )
        if match:
            value = int(match.group(1).replace(",", ""))
            return value, f"visible text match: {match.group(0)!r}"

    # --------------------------------------------------------
    # 4. Raw HTML fallback (inlined JSON in scripts, etc).
    # --------------------------------------------------------

    html_patterns = [
        r'"roomsCompleted"\s*:\s*(\d+)',
        r'"rooms_completed"\s*:\s*(\d+)',
        r'"completedRooms"\s*:\s*(\d+)',
        r'"completed_rooms"\s*:\s*(\d+)',
    ]

    for pattern in html_patterns:
        match = re.search(pattern, html or "", flags=re.IGNORECASE)
        if match:
            value = int(match.group(1))
            return value, f"raw html match: {match.group(0)!r}"

    return None, None

# ============================================================
# Student collection
# ============================================================

def collect_student(username, page):
    print(f"      Checking TryHackMe: {username}")

    html, visible_text, captured_json, status = fetch_profile(
        username, page
    )

    if DEBUG:
        print(f"      [DEBUG] fetch status: {status}")
        print(
            f"      [DEBUG] captured {len(captured_json)} "
            f"JSON response(s)"
        )
        for index, blob in enumerate(captured_json):
            dumped = json.dumps(blob, indent=2)[:1500]
            print(f"      [DEBUG] json[{index}] (truncated):\n{dumped}")
        snippet = (visible_text or "")[:1500]
        print(f"      [DEBUG] visible text (truncated):\n{snippet}")

    if html is None:
        return {
            "username": username,
            "rooms": None,
            "status": status,
        }

    rooms, source = extract_room_count(html, visible_text, captured_json)

    if DEBUG:
        print(f"      [DEBUG] extracted rooms={rooms} from {source}")

    if rooms is None:
        return {
            "username": username,
            "rooms": None,
            "status": "count_not_found",
        }

    return {
        "username": username,
        "rooms": rooms,
        "status": "ok",
    }

# ============================================================
# Markdown
# ============================================================

def format_thm_value(username, rooms, status):
    """
    Generate the value inserted into the TryHackMe column.
    """

    if not username:
        return "—"

    profile = PROFILE_URL.format(username=username)

    if status == "ok":
        return f"[**{rooms} Rooms**]({profile})"

    if status == "count_not_found":
        return f"[Profile]({profile})"

    if status == "profile_not_found":
        return "Profile not found"

    if status == "rate_limited":
        return "Rate limited"

    return f"[Profile]({profile})"


def find_tryhackme_column(header_line):
    """
    Find the column index of 'TryHackMe'.
    """

    cells = [
        cell.strip()
        for cell in header_line.strip().strip("|").split("|")
    ]

    for index, cell in enumerate(cells):
        if cell.lower() == "tryhackme":
            return index

    return None


def update_markdown(markdown_file, results):
    """
    Update ONLY the TryHackMe column.
    The rest of the Markdown table is preserved.
    """

    if not markdown_file.exists():
        raise FileNotFoundError(
            f"Markdown file not found: {markdown_file}"
        )

    content = markdown_file.read_text(encoding="utf-8")
    lines = content.splitlines()

    # --------------------------------------------------------
    # Find the header.
    # --------------------------------------------------------

    header_index = None
    thm_column = None

    for index, line in enumerate(lines):
        if "|" in line and "TryHackMe" in line:
            column = find_tryhackme_column(line)
            if column is not None:
                header_index = index
                thm_column = column
                break

    if header_index is None:
        raise ValueError(
            f"Could not find TryHackMe column in {markdown_file}"
        )

    print(f"      TryHackMe column index: {thm_column}")

    # --------------------------------------------------------
    # Update student rows.
    # --------------------------------------------------------

    updated = 0

    for index in range(header_index + 2, len(lines)):
        line = lines[index]

        if "|" not in line:
            continue

        stripped = line.strip()

        if not stripped:
            continue

        # Skip separator rows.
        if re.match(
            r"^\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)+\|?$",
            stripped
        ):
            continue

        cells = [
            cell.strip()
            for cell in stripped.strip("|").split("|")
        ]

        if len(cells) <= thm_column:
            continue

        register_number = cells[0].strip()

        if register_number not in results:
            continue

        student = results[register_number]

        cells[thm_column] = format_thm_value(
            student["username"],
            student["rooms"],
            student["status"],
        )

        lines[index] = "| " + " | ".join(cells) + " |"

        updated += 1

    markdown_file.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8"
    )

    return updated

# ============================================================
# JSON output
# ============================================================

def save_json(batch_name, results):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    output_file = OUTPUT_DIR / f"{batch_name}_tryhackme.json"

    data = {
        "batch": batch_name,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "students": results,
    }

    output_file.write_text(
        json.dumps(data, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )

    print(f"      JSON: {output_file}")

# ============================================================
# Process batch
# ============================================================

def process_batch(batch_name, config, page):
    print()
    print("=" * 60)
    print(f"Processing {batch_name}")
    print("=" * 60)

    students = load_students(config["csv"])

    print(f"Students found: {len(students)}")

    results = {}

    for register_number, student in students.items():
        username = student["username"]

        if not username:
            results[register_number] = student
            continue

        result = collect_student(username, page)

        results[register_number] = result

        print(
            f"      Result: "
            f"{result['rooms']} "
            f"({result['status']})"
        )

        time.sleep(REQUEST_DELAY)

    save_json(batch_name, results)

    updated = update_markdown(config["markdown"], results)

    print(f"      Markdown rows updated: {updated}")

# ============================================================
# Main
# ============================================================

def main():

    print("TryHackMe Progress Collector")
    print(f"Repository: {ROOT}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent=USER_AGENT)

        try:
            for batch_name, config in BATCHES.items():
                try:
                    process_batch(batch_name, config, page)
                except Exception as exc:
                    print(
                        f"\nERROR processing {batch_name}: {exc}",
                        file=sys.stderr
                    )
                    raise
        finally:
            browser.close()

    print()
    print("=" * 60)
    print("Collection completed.")
    print("=" * 60)


if __name__ == "__main__":
    main()