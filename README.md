# 🤖 Telegram RDP Control Bot

Ye Telegram bot ek **remote Windows machine (RDP)** ko control karta hai. Bot Render par host hota hai, Telegram se aaye commands ko Windows desktop par GUI actions me badal kar chalata hai — app kholna, Chrome me search, website kholo, PowerShell command, desktop ki photo — sab kuch.

**Example flow:** `open chrome` → `search instagram` → `install adblock` → `/screen` se photo check.

## Archivture (kaise kaam karta hai)

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
                        Windows machine (RDP :3389)
```

- [`simple-rdp`](https://github.com/abi-jey/simple-rdp) — asyncio RDP library: screenshot (PIL), keyboard/mouse input, NLA support.
- Actions: `win+r → type → enter`, `ctrl+l`, clicks, typing (unicode — Hindi bhi chalega), scroll, drag.

## ⚙️ Setup (teen cheezein chahiye)

### 1. Windows machine jisme RDP on ho
- **Settings → System → Remote Desktop → Enable** (Windows 10/11 Pro/Enterprise; Home par RDP server nahi hota — VM use karo).
- Us machine ka **username + strong password**.
- Machine internet par reachable honi chahiye port **3389** par:
  - Cloud VM (AWS/Azure/Google/Vultr...) → security group/firewall me 3389 open.
  - Ghar ka PC → router par port-forward 3389 (public IP `whatismyip` se), **ya better: Tailscale/VPN** use karo.
- 🔒 **Security:** NLA (Network Level Authentication) **on** rakho (default hai). 3389 ko poori internet par expose karna risky hai — strong password + `ALLOWED_USERS` zaroor lagao.

### 2. Telegram bot token
- Telegram me [@BotFather](https://t.me/BotFather) → `/newbot` → naam do → **token** copy karo.
- Apna Telegram numeric ID [@userinfobot](https://t.me/userinfobot) se le lo (ALLOWED_USERS ke liye).

### 3. Render account — [render.com](https://render.com) (free signup)

## ☁️ Render par deploy

**Zaroori:** Worker 24/7 online rehta hai. Render **free web services 15 min idle par sleep** ho jate hain aur **free tier par worker nahi hota** — isliye **Starter plan ($7/mo)** recommended hai. Free me chalana hai to alag tarika nahi, bot baar-baar soke uthta rahega.

**Option A — render.yaml (one-click):**
1. Poora folder Git repo banake GitHub par push karo.
2. Render dashboard → **New + → Worker → Blueprint** → repo connect → `render.yaml` pick → Create.
3. Env vars bharo (TELEGRAM_TOKEN, RDP_HOST, RDP_USER, RDP_PASS, ALLOWED_USERS).

**Option B — manual:**
1. Render dashboard → **New + → Worker** → repo connect.
2. Runtime: **Python**, Build: `pip install -r requirements.txt`, Start: `python bot.py`.
3. Plan: **Starter**. Env vars neeche jaise.

### Environment variables

| Variable | Meaning |
|---|---|
| `TELEGRAM_TOKEN` | BotFather ka token (required) |
| `RDP_HOST` | Windows machine ka IP/hostname |
| `RDP_PORT` | default `3389` |
| `RDP_USER` / `RDP_PASS` | Windows login |
| `RDP_DOMAIN` | Domain (usually khaali) |
| `ALLOWED_USERS` | Sirf in Telegram IDs ko allow (comma separated). Khaali = sab allow ⚠️ |
| `RDP_WIDTH` / `RDP_HEIGHT` | Screen size — default 1366×768 |
| `CAPTURE_FPS` | Screen capture rate — default 5 (CPU bachane ke liye kam) |
| `TYPE_CPS` | Typing speed chars/sec — default 12 |

## 💻 Local chalane ke liye (testing)

```bash
cd telegram-rdp-bot
python -m venv venv && source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env    # apni values bharo
python bot.py
```

Telegram me bot ko `/start` bhejo → help dikhega. Phir `/connect` bhejo → bot IP, phir username, phir password poochega (ek-ek karke) → connect! Chaho to `/connect IP USER PASS` bhi likh sakte ho. Uske baad `open chrome` → `/screen`.

## 📋 Commands

**Connection / screen**
- `/connect` — **step-by-step login**: bot ek-ek karke poochega → IP → username → password, phir connect kar dega
- `/connect ip user pass` — agar sab ek saath dena ho
- `/cancel` — connect flow beech me band
- `/disconnect`, `/status`
- `/screen` — desktop ki photo (plain text me "screen" bhi chalega)

> ⚠️ Step-by-step flow me password chat history me save ho jayega — connect ke baad wo message delete kar dena, ya env vars (`RDP_HOST`/`RDP_USER`/`RDP_PASS`) use karo.

**Apps / web**
- `/open chrome` — koi bhi app Run dialog se (notepad, cmd, powershell, calc, mspaint, taskmgr...)
- `/open instagram` ya `/instagram` — website kholna
- `/search instagram` — Chrome me Google search
- `/goto instagram.com` — koi bhi URL
- `/install adblock` — Chrome Web Store search kholo; `/screen` se dekho, phir `/click x y` se "Add to Chrome"
- `/cmd ipconfig` — PowerShell me command chalao (output dekhne ke liye `/screen`)

**Input**
- `/type hello` — text likhna (Hindi/unicode bhi)
- `/key ctrl+shift+esc` — koi bhi shortcut (`win+r`, `alt+f4`, `win+l`, `f11`...)
- `/click 640 360 [right|middle|double]`
- `/move 640 360`, `/scroll down 3`
- `/seq win+r; type chrome; press enter; wait 2; type instagram.com; press enter` — custom chain

**Hinglish bhi chalta hai:** `chrome kholo`, `instagram kholo`, `search instagram`, `adblock install`.

## 🧪 Tests

```bash
python -m unittest test_actions -v
```

Parser + action engine ke unit tests (real RDP server ki zaroorat nahi).

## ❓ Troubleshooting

- **Connection failed / refused** → `RDP_HOST` sahi hai? port 3389 open hai? Windows Firewall RDP allow hai?
- **NLA/CredSSP error** → Windows par Remote Desktop NLA ke saath on hona chahiye (default). Library NLA support karta hai.
- **Bot jawab nahi de raha** → Render free tier sleep; Starter worker lo. `/status` try karo.
- **Chrome nahi khul raha** → Windows par Chrome installed hai? `/open chrome` ke baad `/screen` se check karo.
- **Click coordinate galat** → `/screen` se sahi (x, y) nikaal ke `/click` karo (screen size 1366×768 hai by default).

## ⚠️ Disclaimer

Ye bot poori machine par full control de sakta hai — kisi ko apna `RDP_PASS` ya bot token mat do. `ALLOWED_USERS` hamesha set rakho.
