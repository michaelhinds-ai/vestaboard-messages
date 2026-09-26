#!/usr/bin/env python3
"""
Always-on Vestaboard worker: message rotation + live football score flashes.

- Rotates each board's messages exactly like vestaboard_rotate.py (messages,
  hours and interval are imported from there, so edit them in one place).
- Polls ESPN's scoreboard. When a game involving a tracked team has a score
  change, BOTH boards show the game + score for FLASH_SECONDS, then go back
  to their rotation message.
- Also flashes each tracked game's FINAL once.

Run:  VESTA_TOKEN_1=... VESTA_TOKEN_2=... python vestaboard_live.py
Only needs the Python standard library.
"""

import json
import time
import urllib.request
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from vestaboard_rotate import (
    BOARDS, TIMEZONE, ROWS, COLS, CHAR_MAP,
    index_for, in_window, post_to_board,
)

# ---------------------------------------------------------------------------
# SPORTS SETTINGS
# ESPN team IDs -> name shown on the board.
# ---------------------------------------------------------------------------
COLLEGE_TEAMS = {
    97:   "LOUISVILLE",
    96:   "KENTUCKY",
    2633: "TENNESSEE",
    251:  "TEXAS",
    194:  "OHIO STATE",
    145:  "OLE MISS",
    61:   "GEORGIA",
}
NFL_TEAMS = {
    10: "TITANS",
    4:  "BENGALS",
    17: "PATRIOTS",
    21: "EAGLES",
    2:  "BILLS",
}

FLASH_SECONDS        = 30     # how long a score stays up
POLL_LIVE_SECONDS    = 15     # poll rate while a tracked game is in progress
POLL_IDLE_SECONDS    = 300    # poll rate when no tracked game is live
FLASH_OPPONENT_SCORES = True  # also flash when the other team scores
FLASH_FINALS         = True   # flash "FINAL" once when a tracked game ends
SPORTS_ONLY_IN_WINDOW = True  # only flash during the rotation hours
PAT_SUPPRESS_SECONDS = 300    # a 1- or 2-pt score right after a TD updates silently

FEEDS = [
    ("college", "https://site.api.espn.com/apis/site/v2/sports/football/"
                "college-football/scoreboard?groups=80&limit=300", COLLEGE_TEAMS),
    ("nfl",     "https://site.api.espn.com/apis/site/v2/sports/football/"
                "nfl/scoreboard", NFL_TEAMS),
]
# ---------------------------------------------------------------------------


def log(msg):
    now = datetime.now(ZoneInfo(TIMEZONE))
    print(f"{now:%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------- rendering -------------------------------------

def lines_to_text(lines):
    """Pre-laid-out lines. Rotation's renderer collapses spaces, so score rows
    are padded to full width here and survive intact (22 chars = no shift)."""
    return "\n".join(lines)


def raw_matrix(lines):
    """Place lines verbatim, centering any shorter than 22 chars."""
    lines = [l.upper()[:COLS] for l in lines][:ROWS]
    grid = [[0] * COLS for _ in range(ROWS)]
    top = (ROWS - len(lines)) // 2
    for i, ln in enumerate(lines):
        start = (COLS - len(ln)) // 2
        for j, ch in enumerate(ln):
            grid[top + i][start + j] = CHAR_MAP.get(ch, 0)
    return grid


def score_row(name, score):
    return f"{name[:18]:<19}{str(score):>3}"


# ---------------------------- ESPN parsing ----------------------------------

def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "vestaboard-live/1.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def clock_text(status):
    t = status.get("type", {})
    state, name = t.get("state"), t.get("name", "")
    if state == "post":
        return "FINAL" + (" OT" if status.get("period", 0) > 4 else "")
    if name == "STATUS_HALFTIME":
        return "HALFTIME"
    if state == "in":
        p = status.get("period", 0)
        q = f"Q{p}" if p <= 4 else "OT"
        return f"{q}  {status.get('displayClock', '')}".strip()
    return ""


def parse_games(data, tracked):
    """Yield dicts for games involving a tracked team."""
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        teams = []
        for c in comp.get("competitors", []):
            t = c.get("team", {})
            try:
                tid = int(t.get("id"))
            except (TypeError, ValueError):
                continue
            name = tracked.get(tid) or (t.get("location") or t.get("shortDisplayName") or "").upper()
            try:
                score = int(c.get("score") or 0)
            except ValueError:
                score = 0
            teams.append({"id": tid, "name": name, "score": score,
                          "homeAway": c.get("homeAway")})
        if len(teams) != 2 or not any(t["id"] in tracked for t in teams):
            continue
        teams.sort(key=lambda t: t["homeAway"] != "away")   # away first, home second
        status = comp.get("status") or ev.get("status") or {}
        yield {"id": ev.get("id"), "teams": teams,
               "state": status.get("type", {}).get("state"),
               "clock": clock_text(status), "tracked": tracked}


def headline_for(delta):
    if delta >= 6:
        return "TOUCHDOWN"
    if delta == 3:
        return "FIELD GOAL"
    if delta == 2:
        return "SAFETY"
    return "SCORE"


# ---------------------------- score tracking --------------------------------

class ScoreWatcher:
    def __init__(self):
        self.games = {}      # event id -> {"scores": {tid: n}, "state": s}
        self.last_td = {}    # (event id, tid) -> timestamp
        self.any_live = False

    def poll(self):
        flashes, live = [], False
        for label, url, tracked in FEEDS:
            if not tracked:
                continue
            try:
                data = fetch(url)
            except Exception as e:  # noqa: BLE001
                log(f"[{label}] fetch failed: {e}")
                continue
            for g in parse_games(data, tracked):
                live |= g["state"] == "in"
                flashes += self._diff(g)
        self.any_live = live
        return flashes

    def _diff(self, g):
        eid, now = g["id"], time.time()
        scores = {t["id"]: t["score"] for t in g["teams"]}
        prev = self.games.get(eid)
        self.games[eid] = {"scores": scores, "state": g["state"]}
        if prev is None:                      # first sighting: seed, don't flash
            return []

        out = []
        for t in g["teams"]:
            delta = t["score"] - prev["scores"].get(t["id"], t["score"])
            if delta <= 0:
                continue
            if not FLASH_OPPONENT_SCORES and t["id"] not in g["tracked"]:
                continue
            if delta in (1, 2) and now - self.last_td.get((eid, t["id"]), 0) < PAT_SUPPRESS_SECONDS:
                log(f"PAT/2PT {t['name']} +{delta} (silent)")
                continue
            if delta >= 6:
                self.last_td[(eid, t["id"])] = now
            out.append(self._card(headline_for(delta), t["name"] + "!", g))

        if FLASH_FINALS and g["state"] == "post" and prev["state"] != "post":
            out.append(self._card("FINAL", "", g))
        return out

    @staticmethod
    def _card(headline, sub, g):
        a, h = g["teams"]
        lines = [headline, sub, "",
                 score_row(a["name"], a["score"]),
                 score_row(h["name"], h["score"]),
                 g["clock"]]
        log(f"FLASH: {headline} {sub} | {a['name']} {a['score']} - {h['name']} {h['score']} {g['clock']}")
        return lines


# ---------------------------- board posting ---------------------------------

def post_matrix_all(grid):
    """Post one grid to every board (score flashes)."""
    import vestaboard_rotate as vr
    original = vr.text_to_matrix
    vr.text_to_matrix = lambda _t: grid        # reuse the tested poster
    try:
        for b in BOARDS:
            tok = os.environ.get(b["token_env"])
            if tok:
                post_to_board(tok, "", b["name"])
    finally:
        vr.text_to_matrix = original


def post_rotation(mins):
    for b in BOARDS:
        tok = os.environ.get(b["token_env"])
        if tok:
            msgs = b["messages"]
            post_to_board(tok, msgs[index_for(mins, len(msgs))], b["name"])


# ---------------------------- main loop -------------------------------------

def main():
    if not any(os.environ.get(b["token_env"]) for b in BOARDS):
        raise SystemExit("No board tokens set (VESTA_TOKEN_1 / VESTA_TOKEN_2).")

    watcher = ScoreWatcher()
    queue, shown_idx, next_poll = [], None, 0.0
    log("Vestaboard live worker started")

    while True:
        now = datetime.now(ZoneInfo(TIMEZONE))
        mins = now.hour * 60 + now.minute
        active = in_window(mins)

        if time.time() >= next_poll and (active or not SPORTS_ONLY_IN_WINDOW):
            queue += watcher.poll()
            next_poll = time.time() + (POLL_LIVE_SECONDS if watcher.any_live else POLL_IDLE_SECONDS)

        if queue:
            post_matrix_all(raw_matrix(queue.pop(0)))
            time.sleep(FLASH_SECONDS)
            shown_idx = None                     # force rotation to restore
            continue

        if active:
            idx = index_for(mins, len(BOARDS[0]["messages"]))
            if idx != shown_idx:
                post_rotation(mins)
                shown_idx = idx
        else:
            shown_idx = None

        time.sleep(5)


if __name__ == "__main__":
    main()
