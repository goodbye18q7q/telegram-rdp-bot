# 🤖 Telegram RDP Control Bot

A Telegram bot that controls a **remote Windows machine over RDP**. It runs on
Render (free tier), turns your Telegram messages into Windows desktop actions —
open apps, search in Chrome, open websites, run PowerShell commands, and send
you a photo of the desktop.

**Example flow:** `/connect` → `/win chrome` → `/search instagram` → `/install adblock` → `/screen`

## How it works

```
Telegram  ──messages──▶  bot.py (Render, python-telegram-bot)
                              │
                              ▼
                        commands.py  →  GUI action plan
                              │
                              ▼
                        rdp_manager.py  →  simple-rdp (async RDP client, NLA/CredSSP)
                              │
                              ▼
                        Windows machine (RDP)
```

- [`simple-rdp`](https://github.com/abi-jey/simple-rdp) — asyncio RDP library: screenshots (PIL),
  keyboard/mouse input, NLA/CredSSP support.
- Actions: `win+r → type → enter`, `ctrl+l`, clicks, typing (unicode), scroll, drag.

## ⚙️ Setup

### 1. A Windows machine with RDP enabled
- **Settings → System → Remote Desktop → Enable** (Windows 10/11 Pro/Enterprise;
  Windows Home cannot host RDP — use a VM).
- Note its **username + strong password**.
- It must be reachable on the internet:
  - Cloud VM → open port **3389** in the firewall / security group.
  - Home PC → port-forward 3389 on the router (public IP via `whatismyip`), or better use Tailscale/VPN.
- 🔒 **Security:** keep **NLA** (Network Level Authentication) **on** (default).
  Exposing 3389 to the whole internet is risky — use a strong password and set `ALLOWED_USERS`.

### 2. A Telegram bot token
- Open [@BotFather](https://t.me/BotFather) → `/newbot` → copy the **token**.
- Get your numeric Telegram ID from [@userinfobot](https://t.me/userinfobot) (for `ALLOWED_USERS`).

### 3. A Render account — [render.com](https://render.com)

## ☁️ Deploy on Render (free)

This bot is deployed as a **free web service** with a health-check endpoint and a
GitHub Actions keep-alive ping (every 5 min), because Render free services sleep
after 15 minutes of inactivity.

**One-click (Blueprint):**
1. Push this folder to a GitHub repo.
2. Render dashboard → **New + → Web Service → Blueprint** → connect the repo → pick `render.yaml`.
3. Fill in the env vars.

**Manual:**
1. Render dashboard → **New + → Web Service** → connect the repo.
2. Runtime **Python**, Build `pip install -r requirements.txt`, Start `python bot.py`.
3. Set the plan to **Free**, add the env vars below.

### Environment variables

| Variable | Meaning |
|---|---|
| `TELEGRAM_TOKEN` | Token from BotFather (required) |
| `RDP_HOST` | IP/hostname of the Windows machine (optional — `/connect` asks for it, and `/add` saves profiles) |
| `RDP_PORT` | default `3389` |
| `RDP_USER` / `RDP_PASS` | Windows login (optional — `/connect` asks for it) |
| `ALLOWED_USERS` | Only these Telegram IDs may control the bot (comma separated). Empty = allow everyone ⚠️ |
| `RDP_WIDTH` / `RDP_HEIGHT` | Screen size — default 1366×768 |
| `CAPTURE_FPS` | Screen capture rate — default 5 (keeps CPU low) |
| `RDP_IDLE_KEEPALIVE_SECONDS` | While idle, nudge the pointer 1 pixel and return it (no click/key) every N seconds — default 120; set `0` to disable |
| `TYPE_CPS` | Typing speed (chars/sec) — default 12 |

The RDP manager also enables OS TCP keepalive probes on the RDP socket. Idle input keepalive nudges the pointer 1 pixel and returns it, with no clicks or keystrokes, to reduce the chance of Windows idle-session timeout. `/keepalive` sends one nudge immediately. This cannot override a server-enforced maximum session duration, logoff policy, reboot, or a Render process restart.

## 💻 Run locally (testing)

```bash
cd telegram-rdp-bot
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # fill in your values
python bot.py
```

Then send `/start` to your bot, then `/connect` and follow the prompts.

## 📋 Commands

**Connection / profiles (multiple RDPs)**
- `/add RDP1 1.2.3.4:3389 user pass` — save a profile (host may include `:port`)
- `/connect RDP1` — connect to a saved profile (**always a fresh connection** — any
  existing connection to that profile is closed first)
- `/connect` — **step-by-step login**: the bot asks for IP → username → password,
  and saves it as the next free name (`RDP1`, `RDP2`, ...)
- `/connect ip user pass` — or give everything at once
- `/rdp RDP1` — switch the active profile (`/rdp1`, `rdp 1`, `switch1`, `switch 1` also work)
- `/list` — all profiles with active/connected status
- `/remove RDP1` — delete a profile
- `/cancel` — cancel the connect flow
- `/disconnect`, `/status`
- `/screen` — photo of the active desktop (plain text `screen` works too)

> Profiles are stored in `profiles.json` next to `bot.py` (gitignored — it holds
> passwords). On Render's free tier the file is ephemeral, so profiles reset on
> redeploy; re-add them with `/add`.

**Search / apps / web**
- `/win chrome` — open **Run (Win+R)** and search for an app or name. Any text already
  in the box is **cleared first** (Ctrl+A → Backspace), so it always types your word.
  The bot replies **`Opened: <word>`** or **`Not found: <word>`** by comparing
  the screen before and after pressing Enter.
- `/ctrl hello` — **Ctrl+A** then type text into the focused app
- `/edge` — open **Microsoft Edge** (also `/open edge`, plain `edge`)
- `/open chrome` — open any app (notepad, cmd, powershell, calc, mspaint, taskmgr...)
- `/open instagram` or `/instagram` — open a website
- `/search instagram` — Google search in Chrome
- `/goto instagram.com` — open any URL
- `/install adblock` — open the Chrome Web Store search; use `/screen` then `/click x y`
- `/cmd ipconfig` — run a command in PowerShell (check the result with `/screen`)

**Input**
- `/type hello` — type text (unicode works too)
- `/key ctrl+shift+esc` — any shortcut (`win+r`, `alt+f4`, `win+l`, `f11`...)
- `/click 640 360 [right|middle|double]`
- `/move 640 360`, `/scroll down 3`
- `/seq win+r; type chrome; press enter; wait 2; type instagram.com; press enter` — custom chain

## 🧪 Tests

```bash
python -m unittest discover -p "test_*.py" -v
```

Parser + action engine + handler wiring unit tests (no real RDP server needed).

## ❓ Troubleshooting

- **Connection failed / refused** → is `RDP_HOST` correct? Is port 3389 (or your custom
  port) open? Does Windows Firewall allow Remote Desktop?
- **NLA / CredSSP error** → RDP must be enabled **with NLA** on Windows (default).
- **`Expected Demand Active PDU, got type 0x17`** → some servers (RD Gateway, xrdp,
  hardened Windows) send a Data PDU before the Demand Active PDU. The bot patches
  simple-rdp to skip those PDUs and retry the connection, so this is handled
  automatically. If it still fails, send `/connect` again (each attempt is fresh).
- **Bot does not answer** → free Render services sleep; the keep-alive ping keeps it
  awake. Check the GitHub Actions workflow runs.
- **Chrome does not open** → is Chrome installed? Check with `/screen` after `/open chrome`.
- **Wrong click coordinates** → get the right (x, y) from `/screen` (screen is 1366×768 by default).
- **`Not found` for a real app** → the detection compares screenshots; a very small window
  can look like "no change". Use `/open <app>` for a guaranteed launch.
- **Screen is black in `/screen`** → the bot always connects with `show_wallpaper=True`.
  simple-rdp's default (`False`) sends `PERF_DISABLE_WALLPAPER`, and some servers then
  render a solid black desktop instead of the real one.
- **Windows key does nothing** → many hosted/kiosk RDP setups block the Win key. `/win`
  therefore opens the Start menu with `ctrl+esc` and types the app name there (it also
  retries a few times if the menu does not appear).
- **Login fails after a few tries** → the server may rate-limit new connections; wait a
  few seconds and run `/connect` again (every `/connect` is a fresh connection).

## ⚠️ Disclaimer

This bot can fully control the machine it connects to — never share your `RDP_PASS`
or bot token, and always set `ALLOWED_USERS`.
