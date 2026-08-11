# Vestaboard rotating messages

Posts a rotating set of **4 messages** to **two Vestaboards** every **10 minutes**,
only during **10:00 AM – 9:00 PM US Eastern**, using GitHub Actions (free, runs in
the cloud — your computer does not need to be on).

```
Advances one message every 10 minutes, cycling through the list:
msg 1 → msg 2 → msg 3 → msg 4 → msg 1 → ...
```

The rotation stays correct through daylight-saving changes automatically, because
the script checks Eastern time itself rather than relying on the schedule's clock.

---

## What's in here

| File | What it does |
|------|--------------|
| `vestaboard_rotate.py` | Picks the message for the current 15-min slot and posts it to both boards. Edit your 4 messages at the top. |
| `.github/workflows/vestaboard.yml` | Runs the script every 10 minutes on GitHub's servers. |

Your board tokens are **not** stored in these files — they're added to GitHub as
encrypted "secrets" (step 3 below).

---

## One-time setup (~5 minutes)

### 1. Messages
Each board has its own four messages in `vestaboard_rotate.py`:
`LEFT_MESSAGES` (board 1) and `RIGHT_MESSAGES` (board 2), in slot order
(:00, :15, :30, :45). **Both boards are already filled in** with your messages —
edit these lists any time to change what shows.

Keep each line ≤ 22 characters and each message ≤ 6 lines — the board is 6 rows of
22. Use `\n` for line breaks. Every line is auto-centered.

### 2. Create a GitHub repository
- Go to https://github.com/new
- Name it anything (e.g. `vestaboard`), set it **Private**, click **Create repository**.
- Upload these two files, keeping the folder structure:
  - `vestaboard_rotate.py`
  - `.github/workflows/vestaboard.yml`
  (Easiest: "uploading an existing file" → drag both in. The `.github/workflows/`
  path is created automatically if you type the filename with slashes.)

### 3. Add your board tokens as secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add two:

| Name | Value |
|------|-------|
| `VESTA_TOKEN_1` | left board token |
| `VESTA_TOKEN_2` | right board token |

### 4. Turn on and test
- Open the **Actions** tab. If prompted, click **"I understand my workflows, enable them."**
- Click **"Vestaboard rotating messages"** → **Run workflow** → toggle **force = true**
  → **Run workflow**. This posts immediately (ignoring the time window) so you can
  confirm both boards flip. Watch the run log — each board should report `OK`.

That's it. From then on it runs itself every 10 minutes, 10 AM–9 PM Eastern.

---

## Changing things later
- **New messages:** edit `LEFT_MESSAGES` / `RIGHT_MESSAGES` in `vestaboard_rotate.py` and commit.
- **Different hours:** change `START_MIN` / `END_MIN` in the script.
- **Different cadence:** the four slots are fixed at :00/:15/:30/:45. For a slower
  rotation, change the `cron` in the workflow (and the `slot_for` logic if needed).
- **Rotate a token:** regenerate it in the Vestaboard app and update the matching
  GitHub secret.

## Running it on your own computer instead
The script is self-contained. Set `VESTA_TOKEN_1` / `VESTA_TOKEN_2` as environment
variables and run `python vestaboard_rotate.py` from any scheduler (Windows Task
Scheduler, cron). Add `--force` to post regardless of the time window (handy for a
quick test).

## Notes
- GitHub's scheduled runs are best-effort and can be a few minutes late under load —
  fine for a message board.
- Vestaboard rate-limits to ~1 message per 15 seconds per board; this posts once per
  board per run, well under the limit.
- The poster tries the current Cloud API first and falls back to the older Read/Write
  API automatically, so it works whichever key type you have. (Yours are Cloud API
  tokens, already verified working.)
