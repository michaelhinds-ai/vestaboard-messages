#!/usr/bin/env python3
"""
Always-on Vestaboard worker: message rotation + live sports flashes.

- Rotates each board's messages exactly like vestaboard_rotate.py (messages,
  hours and interval are imported from there, so edit them in one place).
- Polls ESPN for tracked teams and flashes both boards, then goes back to
  the rotation.

  Football:   kickoff, scores (TD / FG / safety / pick six), turnovers,
              2nd half, nail-biter, overtime, final / win celebration.
  Basketball: tip-off, lead changes, scoring runs, halftime, nail-biter,
              overtime, final / win celebration.
- Between rotations, drops in fun cards (bourbon facts, sayings, trivia)
  from fun_content.py on one board at a time, so a promo is always showing.

Run:  VESTA_TOKEN_1=... VESTA_TOKEN_2=... python vestaboard_live.py
Only needs the Python standard library (+ tzdata on some hosts).
"""

import json
import os
import time
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import random

from vestaboard_rotate import (
    BOARDS, TIMEZONE, ROWS, COLS, CHAR_MAP, INTERVAL_MIN,
    index_for, in_window, post_to_board, wrap_lines,
)
import fun_content

# ===========================================================================
# TEAMS  (ESPN team IDs -> name shown on the board)
# ===========================================================================
COLLEGE_TEAMS = {
    97:   "LOUISVILLE",
    96:   "KENTUCKY",
    2633: "TENNESSEE",
    251:  "TEXAS",
    194:  "OHIO STATE",
    145:  "OLE MISS",
    61:   "GEORGIA",
    130:  "MICHIGAN",
}
NFL_TEAMS = {
    10: "TITANS",
    4:  "BENGALS",
    17: "PATRIOTS",
    21: "EAGLES",
    2:  "BILLS",
}
# Men's college basketball uses the same school IDs as football.
BASKETBALL_TEAMS = dict(COLLEGE_TEAMS)

# Custom win messages (line 1 of the win card). Anything not listed shows
# "<TEAM> WIN!". Keep each under 22 characters.
WIN_MESSAGES = {
    97:  "CARDS WIN!",
    96:  "CATS WIN!",
    2633: "VOLS WIN!",
    251: "HOOK EM! TEXAS WINS",
    194: "BUCKEYES WIN!",
    145: "HOTTY TODDY! WIN!",
    61:  "DAWGS WIN!",
    130: "GO BLUE! WIN!",
    10:  "TITANS WIN!",
    4:   "WHO DEY! WIN!",
    17:  "PATS WIN!",
    21:  "EAGLES WIN!",
    2:   "BILLS WIN!",
}

# ===========================================================================
# SETTINGS
# ===========================================================================
FLASH_SECONDS         = 30    # normal flash length
WIN_SECONDS           = 180   # win celebration length (cut to 30s if other flashes are waiting)
STALE_SECONDS         = 180   # drop queued flashes older than this (busy game days)

POLL_LIVE_SECONDS     = 15    # poll rate while a tracked game is live / about to start
POLL_IDLE_SECONDS     = 300   # poll rate otherwise
PREGAME_FAST_POLL_MIN = 20    # start fast polling this many minutes before a tip/kickoff
SPORTS_ONLY_IN_WINDOW = True  # only flash during the rotation hours

# Football
FLASH_OPPONENT_SCORES = True
FLASH_TURNOVERS       = True
FLASH_SECOND_HALF     = True
PAT_SUPPRESS_SECONDS  = 300   # 1- or 2-pt score right after a TD updates silently
FB_CLOSE_MARGIN       = 8     # nail-biter: within this many points...
FB_CLOSE_SECONDS      = 300   # ...with this much left in the 4th quarter

# Basketball
BB_RUN_MIN            = 8     # flash a scoring run of at least this (8-0)
BB_LEAD_COOLDOWN      = 120   # min seconds between lead-change flashes per game
BB_LEAD_MIN_POINTS    = 10    # ignore lead changes until combined score reaches this
BB_CLOSE_MARGIN       = 5
BB_CLOSE_SECONDS      = 120   # last 2 minutes of the 2nd half

# Fun cards (content lives in fun_content.py)
FUN_ENABLED       = True
FUN_SCHEDULE      = {3: 0, 7: 1}  # minute within each rotation cycle -> board (0 = left, 1 = right)
FUN_SECONDS       = 90            # how long a fact / saying stays up
TRIVIA_Q_SECONDS  = 60            # trivia question...
TRIVIA_A_SECONDS  = 45            # ...then the answer

FEEDS = [
    ("college-fb", "football",
     "https://site.api.espn.com/apis/site/v2/sports/football/college-football/scoreboard?groups=80&limit=300",
     COLLEGE_TEAMS),
    ("nfl", "football",
     "https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard",
     NFL_TEAMS),
    ("college-bb", "basketball",
     "https://site.api.espn.com/apis/site/v2/sports/basketball/mens-college-basketball/scoreboard?groups=50&limit=300",
     BASKETBALL_TEAMS),
]
# ===========================================================================


def log(msg):
    now = datetime.now(ZoneInfo(TIMEZONE))
    print(f"{now:%m-%d %H:%M:%S} {msg}", flush=True)


# ---------------------------- rendering -------------------------------------

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


# ---------------------------- ESPN ------------------------------------------

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/128.0 Safari/537.36"),
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.espn.com/",
}


def fetch(url):
    """Try ESPN's main API host, then its alternate host."""
    last = None
    for u in (url, url.replace("site.api.espn.com", "site.web.api.espn.com")):
        try:
            req = urllib.request.Request(u, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def clock_seconds(txt):
    try:
        if ":" in txt:
            m, s = txt.split(":", 1)
            return int(m) * 60 + float(s)
        return float(txt)
    except (ValueError, AttributeError):
        return None


def clock_text(sport, status):
    t = status.get("type", {})
    state, name = t.get("state"), t.get("name", "")
    p = status.get("period", 0) or 0
    regs = 4 if sport == "football" else 2
    if state == "post":
        return "FINAL" + (" OT" if p > regs else "")
    if name == "STATUS_HALFTIME":
        return "HALFTIME"
    if state == "in":
        clk = status.get("displayClock", "")
        if p > regs:
            n = p - regs
            q = "OT" if n == 1 else f"{n}OT"
        elif sport == "football":
            q = f"Q{p}"
        else:
            q = "1ST HALF" if p == 1 else "2ND HALF"
        return f"{q}  {clk}".strip()
    return ""


def parse_games(data, sport, tracked):
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
            teams.append({"id": tid, "name": name, "score": score, "homeAway": c.get("homeAway")})
        if len(teams) != 2 or not any(t["id"] in tracked for t in teams):
            continue
        teams.sort(key=lambda t: t["homeAway"] != "away")   # away first, home second
        status = comp.get("status") or ev.get("status") or {}
        stype = status.get("type", {})

        sit = comp.get("situation") or {}
        lp = sit.get("lastPlay") or {}
        try:
            poss = int(sit.get("possession")) if sit.get("possession") else None
        except (TypeError, ValueError):
            poss = None
        try:
            start = datetime.fromisoformat((ev.get("date") or "").replace("Z", "+00:00")).timestamp()
        except ValueError:
            start = None

        yield {"id": f"{sport}-{ev.get('id')}", "sport": sport, "teams": teams, "tracked": tracked,
               "state": stype.get("state"), "status_name": stype.get("name", ""),
               "period": status.get("period", 0) or 0,
               "secs": clock_seconds(status.get("displayClock", "")),
               "clock": clock_text(sport, status),
               "start": start, "possession": poss,
               "play_id": lp.get("id"),
               "play_type": ((lp.get("type") or {}).get("text") or "").lower(),
               "play_text": (lp.get("text") or "").lower()}


def fb_headline(delta):
    if delta >= 6:
        return "TOUCHDOWN"
    if delta == 3:
        return "FIELD GOAL"
    if delta == 2:
        return "SAFETY"
    return "SCORE"


def turnover_kind(g):
    t = g["play_type"] + " " + g["play_text"]
    if "intercept" in t:
        return "INTERCEPTION"
    if "fumble" in t and ("opponent" in t or "return" in t or "recovered by" in t):
        return "FUMBLE"
    if "turnover on downs" in t:
        return "TURNOVER ON DOWNS"
    return None


# ---------------------------- game tracking ---------------------------------

def card(headline, sub, g, secs=FLASH_SECONDS):
    a, h = g["teams"]
    lines = [headline, sub, "",
             score_row(a["name"], a["score"]),
             score_row(h["name"], h["score"]),
             g["clock"]]
    log(f"FLASH: {headline} {sub} | {a['name']} {a['score']} - {h['name']} {h['score']} {g['clock']}")
    return {"lines": lines, "secs": secs, "created": time.time()}


class Watcher:
    def __init__(self):
        self.games = {}      # game id -> saved state
        self.any_live = False

    def poll(self):
        out, live = [], False
        soon = time.time() + PREGAME_FAST_POLL_MIN * 60
        for label, sport, url, tracked in FEEDS:
            if not tracked:
                continue
            try:
                data = fetch(url)
            except Exception as e:  # noqa: BLE001
                log(f"[{label}] fetch failed: {e}")
                continue
            for g in parse_games(data, sport, tracked):
                if g["state"] == "in" or (g["state"] == "pre" and g["start"] and g["start"] <= soon):
                    live = True
                out += self._diff(g)
        self.any_live = live
        return out

    # -- shared --------------------------------------------------------------
    def _diff(self, g):
        prev = self.games.get(g["id"])
        scores = {t["id"]: t["score"] for t in g["teams"]}
        if prev is None:                                 # first sighting: seed only
            self.games[g["id"]] = {"scores": scores, "state": g["state"], "period": g["period"],
                                   "status_name": g["status_name"], "play_id": g["play_id"],
                                   "close_done": False, "last_td": {}, "leader": self._leader(g),
                                   "lead_at": 0, "run_team": None, "run_pts": 0, "run_done": True}
            return []

        st = self.games[g["id"]]
        deltas = {t["id"]: t["score"] - prev["scores"].get(t["id"], t["score"]) for t in g["teams"]}
        out = []

        # start of game
        if prev["state"] == "pre" and g["state"] == "in":
            out.append(card("KICKOFF!" if g["sport"] == "football" else "TIP-OFF!", "GAME ON", g))

        # sport specific
        if g["sport"] == "football":
            out += self._football(g, st, deltas)
        else:
            out += self._basketball(g, st, deltas)

        # overtime (entering first OT only) / nail-biter
        regs = 4 if g["sport"] == "football" else 2
        if g["state"] == "in" and prev["period"] <= regs < g["period"]:
            out.append(card("OVERTIME!", "", g))
            st["close_done"] = True
        elif g["state"] == "in" and not st["close_done"] and g["period"] == regs and g["secs"] is not None:
            margin = abs(g["teams"][0]["score"] - g["teams"][1]["score"])
            limit, secs = (FB_CLOSE_MARGIN, FB_CLOSE_SECONDS) if g["sport"] == "football" \
                else (BB_CLOSE_MARGIN, BB_CLOSE_SECONDS)
            if margin <= limit and g["secs"] <= secs:
                mins = secs // 60
                out.append(card("NAIL-BITER!", f"UNDER {mins} MIN", g))
                st["close_done"] = True

        # final / win celebration
        if g["state"] == "post" and prev["state"] != "post":
            out.append(self._final(g))

        st.update({"scores": scores, "state": g["state"], "period": g["period"],
                   "status_name": g["status_name"], "play_id": g["play_id"]})
        return out

    @staticmethod
    def _leader(g):
        a, h = g["teams"]
        if a["score"] == h["score"]:
            return None
        return a["id"] if a["score"] > h["score"] else h["id"]

    def _final(self, g):
        a, h = g["teams"]
        if a["score"] != h["score"]:
            w = a if a["score"] > h["score"] else h
            if w["id"] in g["tracked"]:
                msg = WIN_MESSAGES.get(w["id"], f"{w['name']} WIN!")
                return card(msg, "", g, secs=WIN_SECONDS)
        return card("FINAL", "", g)

    # -- football ------------------------------------------------------------
    def _football(self, g, st, deltas):
        out, now = [], time.time()
        prev_period = st["period"]

        if FLASH_SECOND_HALF and g["state"] == "in" and prev_period <= 2 and 3 <= g["period"] <= 4:
            out.append(card("2ND HALF", "UNDERWAY", g))

        tkind = None
        if FLASH_TURNOVERS and g["play_id"] and g["play_id"] != st["play_id"]:
            tkind = turnover_kind(g)

        scored = False
        for t in g["teams"]:
            d = deltas[t["id"]]
            if d <= 0:
                continue
            if not FLASH_OPPONENT_SCORES and t["id"] not in g["tracked"]:
                continue
            if d in (1, 2) and now - st["last_td"].get(t["id"], 0) < PAT_SUPPRESS_SECONDS:
                log(f"PAT/2PT {t['name']} +{d} (silent)")
                continue
            if d >= 6:
                st["last_td"][t["id"]] = now
            head = fb_headline(d)
            if d >= 6 and tkind == "INTERCEPTION":
                head = "PICK SIX"
            elif d >= 6 and tkind == "FUMBLE":
                head = "SCOOP AND SCORE"
            out.append(card(head, t["name"] + "!", g))
            scored = True

        if tkind and not scored:
            names = {t["id"]: t["name"] for t in g["teams"]}
            who = names.get(g["possession"], "")
            out.append(card(tkind + "!", (who + " BALL") if who else "", g))
        return out

    # -- basketball ----------------------------------------------------------
    def _basketball(self, g, st, deltas):
        out, now = [], time.time()
        names = {t["id"]: t["name"] for t in g["teams"]}

        # halftime
        if g["status_name"] == "STATUS_HALFTIME" and st["status_name"] != "STATUS_HALFTIME":
            out.append(card("HALFTIME", "", g))

        # scoring run
        scorers = [tid for tid, d in deltas.items() if d > 0]
        run_card = None
        if len(scorers) == 2:
            st["run_team"], st["run_pts"], st["run_done"] = None, 0, True
        elif len(scorers) == 1:
            tid = scorers[0]
            if st["run_team"] == tid:
                st["run_pts"] += deltas[tid]
            else:
                st["run_team"], st["run_pts"], st["run_done"] = tid, deltas[tid], False
            if not st["run_done"] and st["run_pts"] >= BB_RUN_MIN:
                run_card = card(f"{st['run_pts']}-0 RUN", names[tid] + "!", g)
                st["run_done"] = True

        # lead change
        lead_card = None
        new_leader = self._leader(g)
        total = sum(t["score"] for t in g["teams"])
        if new_leader is not None:
            if (st["leader"] is not None and new_leader != st["leader"]
                    and total >= BB_LEAD_MIN_POINTS and now - st["lead_at"] >= BB_LEAD_COOLDOWN
                    and g["state"] == "in"):
                lead_card = card("LEAD CHANGE", names[new_leader] + " LEADS", g)
                st["lead_at"] = now
            st["leader"] = new_leader

        # one flash per poll: a run that flips the lead beats a plain lead change
        if run_card:
            out.append(run_card)
        elif lead_card:
            out.append(lead_card)
        return out


# ---------------------------- fun cards -------------------------------------

class FunPicker:
    """Cycles fact -> trivia -> saying, never repeating until a pool runs out."""
    ORDER = ["fact", "trivia", "saying"]

    def __init__(self):
        self.pools = {"fact": [], "trivia": [], "saying": []}
        self.turn = 0

    def _draw(self, kind):
        src = {"fact": fun_content.FACTS, "trivia": fun_content.TRIVIA,
               "saying": fun_content.SAYINGS}[kind]
        if not src:
            return None
        if not self.pools[kind]:
            self.pools[kind] = random.sample(src, len(src))
        return self.pools[kind].pop()

    def next_steps(self):
        """List of (text, seconds) to show on one board."""
        for _ in range(len(self.ORDER)):
            kind = self.ORDER[self.turn % len(self.ORDER)]
            self.turn += 1
            item = self._draw(kind)
            if item is None:
                continue
            if kind == "trivia":
                q, a = item
                return [(f"BOURBON TRIVIA: {q}", TRIVIA_Q_SECONDS),
                        (f"ANSWER: {a}", TRIVIA_A_SECONDS)]
            return [(item, FUN_SECONDS)]
        return []


def check_fun_content():
    """Log any fun card too long for the board (it would get cut off)."""
    texts = list(fun_content.FACTS) + list(fun_content.SAYINGS)
    for q, a in fun_content.TRIVIA:
        texts += [f"BOURBON TRIVIA: {q}", f"ANSWER: {a}"]
    for t in texts:
        if len(wrap_lines(t.upper())) > ROWS:
            log(f"WARNING fun card too long, will be cut off: {t}")


# ---------------------------- board posting ---------------------------------

def post_one(bi, text):
    b = BOARDS[bi]
    tok = os.environ.get(b["token_env"])
    if tok:
        post_to_board(tok, text, b["name"])


def restore_one(bi, mins):
    msgs = BOARDS[bi]["messages"]
    post_one(bi, msgs[index_for(mins, len(msgs))])


def post_matrix_all(grid):
    """Post one grid to every board (reuses the tested poster)."""
    import vestaboard_rotate as vr
    original = vr.text_to_matrix
    vr.text_to_matrix = lambda _t: grid
    try:
        for b in BOARDS:
            tok = os.environ.get(b["token_env"])
            if tok:
                post_to_board(tok, "", b["name"])
    finally:
        vr.text_to_matrix = original


def post_rotation(mins, skip=()):
    for bi, b in enumerate(BOARDS):
        if bi in skip:
            continue
        tok = os.environ.get(b["token_env"])
        if tok:
            msgs = b["messages"]
            post_to_board(tok, msgs[index_for(mins, len(msgs))], b["name"])


# ---------------------------- main loop -------------------------------------

def main():
    if not any(os.environ.get(b["token_env"]) for b in BOARDS):
        raise SystemExit("No board tokens set (VESTA_TOKEN_1 / VESTA_TOKEN_2).")

    watcher, fun = Watcher(), FunPicker()
    queue, shown_idx, next_poll = [], None, 0.0
    showing, show_until, min_until = False, 0.0, 0.0
    board_fun, last_fun_slot = {}, None          # board index -> {"steps": [...], "until": ts}
    log("Vestaboard live worker started")
    check_fun_content()

    while True:
        now = datetime.now(ZoneInfo(TIMEZONE))
        mins = now.hour * 60 + now.minute
        active = in_window(mins)
        t = time.time()

        # keep polling even while a flash is up, so no plays are merged/missed
        if t >= next_poll and (active or not SPORTS_ONLY_IN_WINDOW):
            queue += watcher.poll()
            next_poll = t + (POLL_LIVE_SECONDS if watcher.any_live else POLL_IDLE_SECONDS)

        # drop stale flashes on busy days (win cards are kept)
        fresh = [q for q in queue if q["secs"] > FLASH_SECONDS or t - q["created"] <= STALE_SECONDS]
        if len(fresh) != len(queue):
            log(f"dropped {len(queue) - len(fresh)} stale flash(es)")
        queue = fresh

        # end the current flash (a long win card yields after 30s if others are waiting)
        if showing and (t >= show_until or (queue and t >= min_until)):
            showing, shown_idx = False, None

        if not showing and queue:
            item = queue.pop(0)
            post_matrix_all(raw_matrix(item["lines"]))
            showing = True
            show_until = t + item["secs"]
            min_until = t + FLASH_SECONDS
            board_fun.clear()                    # sports beats fun
        elif not showing and active:
            # rotation (boards showing a fun card catch up when it ends)
            idx = index_for(mins, len(BOARDS[0]["messages"]))
            if idx != shown_idx:
                post_rotation(mins, skip=set(board_fun))
                shown_idx = idx

            # advance / finish fun cards
            for bi in list(board_fun):
                st = board_fun[bi]
                if t >= st["until"]:
                    if st["steps"]:
                        text, secs = st["steps"].pop(0)
                        post_one(bi, text)
                        st["until"] = t + secs
                    else:
                        del board_fun[bi]
                        restore_one(bi, mins)

            # start a fun card on schedule
            slot = (now.hour, now.minute)
            cyc = now.minute % INTERVAL_MIN
            if FUN_ENABLED and cyc in FUN_SCHEDULE and slot != last_fun_slot:
                last_fun_slot = slot
                bi = FUN_SCHEDULE[cyc]
                if bi < len(BOARDS) and bi not in board_fun and os.environ.get(BOARDS[bi]["token_env"]):
                    steps = fun.next_steps()
                    if steps:
                        text, secs = steps.pop(0)
                        log(f"FUN [{BOARDS[bi]['name']}]: {text}")
                        post_one(bi, text)
                        board_fun[bi] = {"steps": steps, "until": t + secs}
        elif not showing:
            shown_idx = None
            board_fun.clear()

        time.sleep(2)


if __name__ == "__main__":
    main()
