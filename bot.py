"""Telegram bot — controls remote Windows machines over RDP.

Supports multiple saved RDP profiles (RDP1, RDP2, ...). Every command acts on
the ACTIVE profile; use /rdp <name> (or just /rdp1) to switch between them.
"""
from __future__ import annotations

import functools
import io
import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

from PIL import ImageChops
from PIL import ImageOps
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import commands
from config import load_config
from rdp_manager import RDPManager

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("rdpbot")

cfg = load_config()

PROFILES_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "profiles.json")

# name -> {"host": ..., "port": ..., "user": ..., "password": ...}
profiles: dict[str, dict] = {}
# The connection target every command acts on
current: dict = {"name": None, "host": "", "port": 3389, "user": "", "password": ""}
# One live RDP connection per profile
managers: dict[str, RDPManager] = {}
# chat_id -> {"step": "ip"|"user"|"pass", "host": ..., "port": ...}
connect_sessions: dict[int, dict] = {}

OPENED_RATIO = 0.15

HELP_TEXT = """🤖 RDP Control Bot — multi profile

🔌 Profiles:
/add RDP1 1.2.3.4:3389 user pass — save a profile (host may include :port)
/connect RDP1                 — connect to a saved profile
/connect                      — step by step (IP → username → password), saves as RDP1, RDP2...
/rdp RDP1   or   /rdp1        — switch the active profile
/list                         — all profiles (active + connected)
/remove RDP1                  — delete a profile
/disconnect                   — close the active connection
/status                       — active profile + connection status
/cancel                       — cancel the connect flow
/screen                       — photo of the active desktop

⌨️ Commands (they act on the ACTIVE profile):
/win chrome — open Run (Win+R) and search for an app or name
/ctrl hello — Ctrl+A then type text into the focused app
/open chrome — open any app (notepad, cmd, powershell, calc, mspaint...)
/open instagram — open a website
/search instagram — Google search in Chrome
/goto instagram.com — open a URL
/instagram — open Instagram directly
/install adblock — open the Chrome Web Store
/cmd ipconfig — run a command in PowerShell
/type hello — type text (unicode works too)
/key ctrl+shift+esc — shortcut (win+r, alt+f4, win+l...)
/click 640 360 — mouse click (right/double also supported)
/scroll down 3
/seq win+r; type chrome; press enter; wait 2; type instagram.com; press enter

Plain examples: "open chrome", "search instagram", "win chrome", "rdp2"
"""


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def load_profiles() -> dict[str, dict]:
    try:
        with open(PROFILES_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_profiles() -> None:
    try:
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(profiles, f, indent=2)
    except Exception as e:
        log.warning("could not save profiles: %s", e)


def next_profile_name() -> str:
    i = 1
    while f"rdp{i}" in profiles:
        i += 1
    return f"rdp{i}"


def profile_cfg(name: str) -> dict:
    p = profiles.get(name, {})
    c = dict(cfg)
    c["rdp_host"] = p.get("host", "")
    c["rdp_port"] = int(p.get("port", 3389))
    c["rdp_user"] = p.get("user", "")
    c["rdp_pass"] = p.get("password", "")
    return c


def mgr_key(name: str | None = None) -> str:
    return (name or current["name"] or "adhoc").lower()


def get_mgr(name: str | None = None) -> RDPManager:
    """Return (creating if needed) the RDP manager for a profile."""
    key = mgr_key(name)
    mgr = managers.get(key)
    if mgr is None:
        if key == "adhoc" and not current["host"]:
            raise ConnectionError("No RDP selected — use /add or /connect first")
        mgr = RDPManager(profile_cfg(key) if key != "adhoc" else _adhoc_cfg())
        managers[key] = mgr
    return mgr


def _adhoc_cfg() -> dict:
    c = dict(cfg)
    c["rdp_host"] = current["host"]
    c["rdp_port"] = current["port"]
    c["rdp_user"] = current["user"]
    c["rdp_pass"] = current["password"]
    return c


def split_host_port(text: str, default_port: int = 3389) -> tuple[str, int]:
    """'1.2.3.4:65430' -> ('1.2.3.4', 65430); '1.2.3.4' -> ('1.2.3.4', 3389)"""
    text = text.strip()
    if text.startswith("["):  # IPv6: [2001:db8::1]:3389
        host, _, rest = text.partition("]")
        port_s = rest.lstrip(":") or str(default_port)
        return host.lstrip("["), int(port_s)
    if ":" in text:
        host, _, port_s = text.rpartition(":")
        try:
            return host.strip(), int(port_s)
        except ValueError:
            pass
    return text, default_port


def diff_ratio(img_a, img_b) -> float:
    """Fraction of pixels that differ by more than 40 (grayscale)."""
    a = ImageOps.grayscale(img_a)
    b = ImageOps.grayscale(img_b)
    if a.size != b.size:
        b = b.resize(a.size)
    hist = ImageChops.difference(a, b).histogram()
    total = a.size[0] * a.size[1]
    return sum(hist[40:]) / total if total else 0.0


def start_health_server() -> None:
    """Tiny HTTP server — health check / free-tier keep-alive ping.

    Render free web services sleep after 15 min of inactivity. A free service
    like UptimeRobot (or the GitHub Actions workflow in this repo) pings this
    URL every 5 minutes so the service stays awake.
    """
    port = int(os.environ.get("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass  # keep it quiet

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on :%s", port)


# Load saved profiles at startup
profiles = load_profiles()


def guarded(handler):
    """Only ALLOWED_USERS may use the bot (empty = allow everyone, with a warning)."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        uname = update.effective_user.username if update.effective_user else None
        log.info(
            "Message from user id=%s username=%s chat=%s",
            uid,
            uname,
            update.effective_chat.id if update.effective_chat else None,
        )
        if not cfg["allowed_users"]:
            log.warning("ALLOWED_USERS is not set — everyone is allowed!")
        elif uid not in cfg["allowed_users"]:
            if update.message:
                await update.message.reply_text("⛔ You are not authorized.")
            return
        return await handler(update, context)

    return wrapper


# ---------------------------------------------------------------------------
# Connection handlers
# ---------------------------------------------------------------------------

async def _do_connect(update: Update, name: str | None = None) -> None:
    try:
        mgr = get_mgr(name)
        await mgr.connect()
        p = profiles.get(mgr_key(name), {})
        host = p.get("host", current["host"])
        port = p.get("port", current["port"])
        await update.message.reply_text(f"✅ Connected: {mgr_key(name)} — {host}:{port}")
    except Exception as e:
        log.exception("connect failed")
        await update.message.reply_text(f"❌ Connection failed:\n{e}")


@guarded
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


@guarded
async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Save a profile: /add <name> <host[:port]> <user> <password>"""
    args = context.args or []
    if len(args) < 4:
        await update.message.reply_text(
            "Usage: /add <name> <host[:port]> <username> <password>\n"
            "Example: /add RDP1 1.2.3.4:3389 Administrator MyPass"
        )
        return
    name = args[0].lower()
    host, port = split_host_port(args[1])
    profiles[name] = {"host": host, "port": port, "user": args[2], "password": args[3]}
    save_profiles()
    await update.message.reply_text(f"💾 Saved profile: {name} — {host}:{port}\nConnect with /connect {name} or /rdp {name}")


@guarded
async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    name = (context.args or [""])[0].lower()
    if name not in profiles:
        await update.message.reply_text(f"Profile not found: {name}")
        return
    profiles.pop(name, None)
    mgr = managers.pop(name, None)
    if mgr is not None:
        try:
            await mgr.disconnect()
        except Exception:
            pass
    if current["name"] == name:
        current.update(name=None, host="", port=3389, user="", password="")
    save_profiles()
    await update.message.reply_text(f"🗑️ Removed profile: {name}")


@guarded
async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not profiles:
        await update.message.reply_text("No profiles saved yet.\nUse /add <name> <host> <user> <pass>")
        return
    lines = []
    for name in sorted(profiles):
        p = profiles[name]
        mgr = managers.get(name)
        state = "🟢 connected" if (mgr and mgr.is_connected) else "⚪ offline"
        marker = "👉 " if current["name"] == name else "   "
        lines.append(f"{marker}{name} — {p.get('host')}:{p.get('port')} ({p.get('user')}) {state}")
    await update.message.reply_text("\n".join(lines))


@guarded
async def cmd_switch(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/rdp <name> — make a profile active and connect to it."""
    name = (context.args or [""])[0].lower()
    if name not in profiles:
        await update.message.reply_text(
            f"Profile not found: {name}\nSaved: {', '.join(sorted(profiles)) or 'none'}\nUse /add to save one."
        )
        return
    current.update(
        name=name,
        host=profiles[name]["host"],
        port=profiles[name]["port"],
        user=profiles[name]["user"],
        password=profiles[name]["password"],
    )
    await _do_connect(update, name)


@guarded
async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    chat_id = update.effective_chat.id

    # /connect <saved profile>
    if len(args) == 1 and args[0].lower() in profiles:
        await cmd_switch(update, context)
        return

    # /connect <host> <user> <pass> — one shot, saved as a new profile
    if len(args) >= 3:
        host, port = split_host_port(args[0])
        name = next_profile_name()
        profiles[name] = {"host": host, "port": port, "user": args[1], "password": args[2]}
        save_profiles()
        current.update(name=name, host=host, port=port, user=args[1], password=args[2])
        connect_sessions.pop(chat_id, None)
        await _do_connect(update)
        return

    # Step-by-step flow
    session = {"step": "ip"}
    if len(args) == 1:
        host, port = split_host_port(args[0])
        session["host"] = host
        session["port"] = port
        session["step"] = "user"
    elif len(args) == 2:
        host, port = split_host_port(args[0])
        session["host"] = host
        session["port"] = port
        session["user"] = args[1]
        session["step"] = "pass"

    connect_sessions[chat_id] = session

    if session["step"] == "ip":
        await update.message.reply_text(
            "🔌 RDP connect — send the IP address first.\n"
            "Port is 3389: send just the IP (1.2.3.4).\n"
            "Different port: send IP:port — e.g. 1.2.3.4:65430.\n"
            "Send /cancel to stop."
        )
    elif session["step"] == "user":
        await update.message.reply_text(f"🌐 IP: {session['host']}  Port: {session['port']}\n👤 Now send the username:")
    else:
        await update.message.reply_text(
            f"🌐 IP: {session['host']}  👤 {session['user']}\n🔑 Now send the password:"
        )


async def _handle_connect_step(update: Update, session: dict) -> None:
    chat_id = update.effective_chat.id
    text = update.message.text.strip()
    step = session["step"]

    if step == "ip":
        host, port = split_host_port(text)
        session["host"] = host
        session["port"] = port
        session["step"] = "user"
        await update.message.reply_text(f"🌐 Host: {host}  Port: {port}\n👤 Now send the username:")
    elif step == "user":
        session["user"] = text
        session["step"] = "pass"
        await update.message.reply_text(
            "🔑 Now send the password:\n"
            "⚠️ The password will be saved in the chat history — delete this message after connecting."
        )
    else:
        host = session.get("host", "")
        port = session.get("port", 3389)
        name = next_profile_name()
        profiles[name] = {"host": host, "port": port, "user": session.get("user", ""), "password": text}
        save_profiles()
        current.update(name=name, host=host, port=port, user=session.get("user", ""), password=text)
        connect_sessions.pop(chat_id, None)
        await _do_connect(update)
        await update.message.reply_text(f"💾 Saved as profile: {name} — switch anytime with /rdp {name}")


@guarded
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if connect_sessions.pop(chat_id, None):
        await update.message.reply_text("❌ Connect flow cancelled.")
    else:
        await update.message.reply_text("No connect flow is running.")


@guarded
async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        await get_mgr().disconnect()
        await update.message.reply_text(f"🔌 Disconnected from {mgr_key()}.")
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


@guarded
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not current["name"] and not current["host"]:
        await update.message.reply_text("No RDP selected. Use /connect or /rdp <name>.")
        return
    mgr = managers.get(mgr_key())
    state = "🟢 connected" if (mgr and mgr.is_connected) else "🔴 disconnected"
    await update.message.reply_text(
        f"👉 Active: {mgr_key()} — {current['host']}:{current['port']} ({current['user']}) {state}"
    )


@guarded
async def cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        img = await get_mgr().screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        await update.message.reply_photo(
            photo=buf, caption=f"🖥 {mgr_key()} — {current['host']} ({img.width}x{img.height})"
        )
    except Exception as e:
        log.exception("screenshot failed")
        await update.message.reply_text(f"❌ Screenshot failed: {e}")


# ---------------------------------------------------------------------------
# Command handlers (act on the ACTIVE profile)
# ---------------------------------------------------------------------------

async def _win_flow(update: Update, word: str) -> None:
    """Win+R + type a word + Enter, then report Opened / Not found."""
    if not word:
        await update.message.reply_text("Not found: no search word given (/win <word>)")
        return
    try:
        mgr = get_mgr()
        await mgr.execute(commands.a_win_search(word))
        before = await mgr.screenshot()
        await mgr.execute([("key", "enter"), ("wait", 1.8)])
        after = await mgr.screenshot()

        ratio = diff_ratio(before, after)
        log.info("win search %r -> changed ratio %.3f", word, ratio)

        if ratio >= OPENED_RATIO:
            await update.message.reply_text(f"Opened: {word}")
        else:
            await update.message.reply_text(f"Not found: {word}")
    except Exception as e:
        log.exception("win search failed")
        await update.message.reply_text(f"Error: {e}")


@guarded
async def cmd_win(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _win_flow(update, " ".join(context.args or []).strip())


async def _ctrl_flow(update: Update, word: str) -> None:
    """Ctrl+A then type — replaces the current selection in the focused app."""
    if not word:
        await update.message.reply_text("Usage: /ctrl <text>")
        return
    try:
        await get_mgr().execute([("key", "ctrl+a"), ("wait", 0.2), ("text", word)])
        await update.message.reply_text(f"Typed: {word}")
    except Exception as e:
        log.exception("ctrl type failed")
        await update.message.reply_text(f"Error: {e}")


@guarded
async def cmd_ctrl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ctrl_flow(update, " ".join(context.args or []).strip())


@guarded
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()

    # /connect step-by-step flow
    session = connect_sessions.get(update.effective_chat.id)
    if session:
        return await _handle_connect_step(update, session)

    low = text.lower()

    # Plain-text shortcuts
    if low in ("screen", "screenshot", "photo", "desktop photo"):
        return await cmd_screen(update, context)
    if low in ("connect", "login"):
        context.args = []
        return await cmd_connect(update, context)
    if low in ("disconnect", "logout"):
        return await cmd_disconnect(update, context)
    if low in ("status",):
        return await cmd_status(update, context)
    if low in ("list", "profiles"):
        return await cmd_list(update, context)
    if low == "win" or low.startswith("win "):
        return await _win_flow(update, text[3:].strip())
    if low == "ctrl" or low.startswith("ctrl "):
        return await _ctrl_flow(update, text[4:].strip())

    # Profile switching: "rdp2", "rdp 2", "rdp rdp2", "/rdp2"
    sw = low[1:] if low.startswith("/") else low
    if sw.startswith("rdp"):
        rest = sw[3:].strip()
        if rest.isdigit():
            rest = f"rdp{rest}"
        if rest:
            context.args = [rest]
            return await cmd_switch(update, context)

    try:
        actions, desc = commands.parse_command(text)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}\n\nType /start to see the commands.")
        return

    try:
        await get_mgr().execute(actions)
        await update.message.reply_text(f"✅ {desc}")
    except Exception as e:
        log.exception("execute failed")
        await update.message.reply_text(f"❌ Error: {e}")


def main() -> None:
    if not cfg["telegram_token"]:
        raise SystemExit("TELEGRAM_TOKEN missing — set it in the environment")

    start_health_server()

    app = Application.builder().token(cfg["telegram_token"]).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("remove", cmd_remove))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("rdp", cmd_switch))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(CommandHandler("win", cmd_win))
    app.add_handler(CommandHandler("ctrl", cmd_ctrl))
    app.add_handler(MessageHandler(filters.TEXT, on_message))

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unhandled error: %s", context.error, exc_info=context.error)

    app.add_error_handler(on_error)
    log.info("Bot is starting... profiles: %s", sorted(profiles))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
