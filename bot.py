"""Telegram bot — controls a remote Windows machine over RDP.

Slash commands and plain text are turned into GUI actions and executed over RDP.
Connection and screenshot commands (/connect, /disconnect, /status, /screen) live here.
"""
from __future__ import annotations

import functools
import io
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
mgr = RDPManager(cfg)

# chat_id -> {"step": "ip"|"user"|"pass", "host": ..., "port": ...}
# Step-by-step /connect flow (IP, then username, then password)
connect_sessions: dict[int, dict] = {}

# Minimum fraction of changed pixels to call an app "opened"
OPENED_RATIO = 0.15

HELP_TEXT = """🤖 RDP Control Bot

🔌 Connection:
/connect — step-by-step login (IP → username → password, one by one)
          You can include the port too: IP:port (e.g. 1.2.3.4:65430)
/connect ip user pass — or give everything at once
/cancel — cancel the connect flow
/disconnect — close the session
/status — connection status
/screen — photo of the desktop

⌨️ Commands (slash and plain text both work):
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

Plain examples: "open chrome", "search instagram", "win chrome"
"""


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


@guarded
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


async def _do_connect(update: Update) -> None:
    try:
        await mgr.connect()
        await update.message.reply_text(f"✅ Connected: {cfg['rdp_host']}:{cfg['rdp_port']}")
    except Exception as e:
        log.exception("connect failed")
        await update.message.reply_text(f"❌ Connection failed:\n{e}")


@guarded
async def cmd_connect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    args = context.args or []
    chat_id = update.effective_chat.id

    # Full credentials given at once: connect directly
    if len(args) >= 3:
        host, port = split_host_port(args[0])
        cfg["rdp_host"] = host
        cfg["rdp_user"] = args[1]
        cfg["rdp_pass"] = args[2]
        if len(args) >= 4:
            try:
                cfg["rdp_port"] = int(args[3])
            except ValueError:
                cfg["rdp_port"] = port
        else:
            cfg["rdp_port"] = port
        connect_sessions.pop(chat_id, None)
        await _do_connect(update)
        return

    # Start the step-by-step flow
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
    elif cfg.get("rdp_host"):
        # Host already set from env — no need to ask for it
        host, port = split_host_port(cfg["rdp_host"])
        session["host"] = host
        session["port"] = port
        session["step"] = "user"

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
        cfg["rdp_host"] = session.get("host") or cfg["rdp_host"]
        cfg["rdp_port"] = session.get("port", cfg["rdp_port"])
        cfg["rdp_user"] = session.get("user", "")
        cfg["rdp_pass"] = text
        connect_sessions.pop(chat_id, None)
        await _do_connect(update)


@guarded
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if connect_sessions.pop(chat_id, None):
        await update.message.reply_text("❌ Connect flow cancelled.")
    else:
        await update.message.reply_text("No connect flow is running.")


@guarded
async def cmd_disconnect(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await mgr.disconnect()
    await update.message.reply_text("🔌 Disconnected.")


@guarded
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(mgr.status_text())


@guarded
async def cmd_screen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        img = await mgr.screenshot()
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        await update.message.reply_photo(
            photo=buf, caption=f"🖥 {cfg['rdp_host']} ({img.width}x{img.height})"
        )
    except Exception as e:
        log.exception("screenshot failed")
        await update.message.reply_text(f"❌ Screenshot failed: {e}")


async def _win_flow(update: Update, word: str) -> None:
    """Win+R + type a word + Enter, then report Opened / Not found."""
    if not word:
        await update.message.reply_text("Not found: no search word given (/win <word>)")
        return
    try:
        # Open Run and type the word (no Enter yet)
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
        await mgr.execute([("key", "ctrl+a"), ("wait", 0.2), ("text", word)])
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

    # /win and /ctrl step-by-step flows
    session = connect_sessions.get(update.effective_chat.id)
    if session:
        return await _handle_connect_step(update, session)

    low = text.lower()

    # Plain-text shortcuts
    if low in ("screen", "screenshot", "photo", "desktop photo"):
        return await cmd_screen(update, context)
    if low in ("connect", "login"):
        return await cmd_connect(update, context)
    if low in ("disconnect", "logout"):
        return await cmd_disconnect(update, context)
    if low in ("status",):
        return await cmd_status(update, context)
    if low == "win" or low.startswith("win "):
        return await _win_flow(update, text[3:].strip())
    if low == "ctrl" or low.startswith("ctrl "):
        return await _ctrl_flow(update, text[4:].strip())

    try:
        actions, desc = commands.parse_command(text)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}\n\nType /start to see the commands.")
        return

    try:
        await mgr.execute(actions)
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
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(CommandHandler("win", cmd_win))
    app.add_handler(CommandHandler("ctrl", cmd_ctrl))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unhandled error: %s", context.error, exc_info=context.error)

    app.add_error_handler(on_error)
    log.info("Bot is starting...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
