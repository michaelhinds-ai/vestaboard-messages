#!/usr/bin/env python3
"""
Vestaboard rotating-message poster (two boards, independent message sets).

Each board rotates through its OWN four messages, chosen by the current
15-minute slot of the hour:

    :00-:14  -> message 1
    :15-:29  -> message 2
    :30-:44  -> message 3
    :45-:59  -> message 4

Only posts during the active window (default 10:00-21:00 America/New_York).
Outside that window it exits quietly without touching the boards.

Board tokens are read from environment variables so they never live in the
code:  VESTA_TOKEN_1 (left board) and VESTA_TOKEN_2 (right board).

Messages are rendered to a centered 6x22 character grid and posted, so the
layout you write is exactly what appears (every line centered).
"""

import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

ROWS, COLS = 6, 22

# ---------------------------------------------------------------------------
# MESSAGES
# One list of four per board, in slot order (:00, :15, :30, :45).
# Use "\n" for line breaks. Every line is auto-centered on the 6x22 board.
# Keep each line <= 22 characters; keep each message <= 6 lines.
# ---------------------------------------------------------------------------

LEFT_MESSAGES = [
    # :00
    "FILL YOUR OWN BOURBON\n"
    "FROM THE BARREL\n"
    "\n"
    "6YR MAPLE BOURBON\n"
    "7 YEAR RYE\n"
    "ONLY $99",

    # :15
    "WHISKEY STRAIGHT FROM\n"
    "THE BARREL!\n"
    "\n"
    "TRY IT NOW!",

    # :30
    "HEY YOU...\n"
    "YES... YOU...\n"
    "\n"
    "TRY ME NOW!\n"
    "WHISKEY FROM\n"
    "THE BARREL",

    # :45
    "LOOKING FOR A GIFT\n"
    "OR SOMETHING SPECIAL?\n"
    "\n"
    "BOTTLE YOUR OWN\n"
    "ONLY $99",
]

RIGHT_MESSAGES = [
    # :00
    "FILL YOUR OWN BOURBON\n"
    "FROM THE BARREL\n"
    "\n"
    "8YR KY BOURBON\n"
    "6YR HONEY BOURBON\n"
    "ONLY $99",

    # :15
    "TRY WHISKEY STRAIGHT\n"
    "FROM THE BARREL!\n"
    "\n"
    "TRY IT NOW!",

    # :30
    "YOU WOULD LOOK\n"
    "REAL COOL WITH A\n"
    "POUR OF THIS\n"
    "IN YOUR HAND",

    # :45
    "LOOKING FOR A GIFT\n"
    "OR SOMETHING SPECIAL?\n"
    "\n"
    "BOTTLE YOUR OWN\n"
    "ONLY $99",
]

BOARDS = [
    {"name": "left",  "token_env": "VESTA_TOKEN_1", "messages": LEFT_MESSAGES},
    {"name": "right", "token_env": "VESTA_TOKEN_2", "messages": RIGHT_MESSAGES},
]

# ---------------------------------------------------------------------------
# Schedule / window settings
# ---------------------------------------------------------------------------
TIMEZONE  = "America/New_York"
START_MIN = 10 * 60          # 10:00 AM  (minutes since midnight)
END_MIN   = 21 * 60          #  9:00 PM
# ---------------------------------------------------------------------------

CLOUD_URL = "https://cloud.vestaboard.com/"
RW_URL    = "https://rw.vestaboard.com/"

# Vestaboard character codes.
CHAR_MAP = {
    " ": 0,
    **{chr(ord("A") + i): i + 1 for i in range(26)},   # A-Z -> 1..26
    "1": 27, "2": 28, "3": 29, "4": 30, "5": 31,
    "6": 32, "7": 33, "8": 34, "9": 35, "0": 36,
    "!": 37, "@": 38, "#": 39, "$": 40, "(": 41, ")": 42,
    "-": 44, "+": 46, "&": 47, "=": 48, ";": 49, ":": 50,
    "'": 52, '"': 53, "%": 54, ",": 55, ".": 56, "/": 59,
    "?": 60, "°": 62,
}


def slot_for(minute: int) -> int:
    return (minute // 15) % 4


def in_window(mins_since_midnight: int) -> bool:
    return START_MIN <= mins_since_midnight <= END_MIN


def wrap_lines(text: str):
    """Split on explicit newlines, then word-wrap each paragraph to COLS."""
    out = []
    for para in text.upper().split("\n"):
        words = para.split()
        if not words:
            out.append("")          # preserve intentional blank lines
            continue
        cur = ""
        for w in words:
            w = w[:COLS]
            if not cur:
                cur = w
            elif len(cur) + 1 + len(w) <= COLS:
                cur += " " + w
            else:
                out.append(cur)
                cur = w
        out.append(cur)
    return out


def text_to_matrix(text: str):
    """Render text to a centered 6x22 grid of Vestaboard character codes."""
    lines = wrap_lines(text)[:ROWS]
    grid = [[0] * COLS for _ in range(ROWS)]
    top = (ROWS - len(lines)) // 2
    for i, ln in enumerate(lines):
        row = top + i
        start = (COLS - len(ln)) // 2
        for j, ch in enumerate(ln):
            grid[row][start + j] = CHAR_MAP.get(ch, 0)
    return grid


def _post(url: str, header_name: str, token: str, payload: dict):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header(header_name, token)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status, resp.read().decode("utf-8", "replace")


def _already_displayed(status, body) -> bool:
    """Vestaboard returns 409 / 'FingerprintMatch' when the board already shows
    this exact message. That's the desired end state, so treat it as success."""
    if status == 409:
        return True
    b = (body or "").lower()
    return "fingerprintmatch" in b or "already" in b


def post_to_board(token: str, text: str, label: str) -> bool:
    """Post a centered grid. Cloud API first, Read/Write API as fallback.
    A message already on the board (HTTP 409) counts as success."""
    matrix = text_to_matrix(text)
    attempts = [
        ("Cloud API", CLOUD_URL, "X-Vestaboard-Token", {"characters": matrix}),
        ("Read-Write API", RW_URL, "X-Vestaboard-Read-Write-Key", {"characters": matrix}),
    ]
    last = ""
    for name, url, header, payload in attempts:
        try:
            status, resp = _post(url, header, token, payload)
            if 200 <= status < 300:
                print(f"[{label}] OK via {name} (HTTP {status})")
                return True
            if _already_displayed(status, resp):
                print(f"[{label}] OK - already on board (via {name}, HTTP {status})")
                return True
            last = f"{name}: HTTP {status} {resp[:200]}"
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            if _already_displayed(e.code, detail):
                print(f"[{label}] OK - already on board (via {name}, HTTP {e.code})")
                return True
            last = f"{name}: HTTP {e.code} {detail}"
        except Exception as e:  # noqa: BLE001
            last = f"{name}: {e}"
    print(f"[{label}] FAILED. Last error -> {last}")
    return False


def main():
    force = "--force" in sys.argv or os.environ.get("VESTA_FORCE") == "1"

    now = datetime.now(ZoneInfo(TIMEZONE))
    mins = now.hour * 60 + now.minute
    slot = slot_for(now.minute)
    print(f"Now: {now:%Y-%m-%d %H:%M %Z}  (minute {now.minute}, slot {slot + 1}/4)")

    if not in_window(mins) and not force:
        print(f"Outside window ({START_MIN//60:02d}:00-{END_MIN//60:02d}:00). Nothing to post.")
        return 0

    ok = True
    posted_any = False
    for board in BOARDS:
        token = os.environ.get(board["token_env"])
        if not token:
            print(f"[{board['name']}] no token in {board['token_env']} - skipping.")
            continue
        posted_any = True
        text = board["messages"][slot]
        print(f"[{board['name']}] slot {slot + 1}: {text.splitlines()[0] if text else ''!r} ...")
        ok = post_to_board(token, text, board["name"]) and ok

    if not posted_any:
        print("ERROR: no board tokens set (VESTA_TOKEN_1 / VESTA_TOKEN_2).", file=sys.stderr)
        return 2
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
