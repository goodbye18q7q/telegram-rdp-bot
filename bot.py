"""Telegram bot — remote Windows machine ko RDP se control karta hai.

Commands (slash ya plain text) GUI actions me badal kar RDP par chalte hain.
Connection commands (/connect, /disconnect, /status, /screen) yahin hain.
"""
from __future__ import annotations

import functools
import io
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer

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

# chat_id -> {"step": "ip"|"user"|"pass", "host": ..., "user": ...}
# /connect ka step-by-step flow (ek-ek karke IP, username, password)
connect_sessions: dict[int, dict] = {}

HELP_TEXT = """🤖 RDP Control Bot

🔌 Connection:
/connect — step-by-step login (IP → username → password ek-ek karke poocha jayega)
          IP me port bhi likh sakte ho: `IP:port` (jaise 1.2.3.4:65430)
/connect ip user pass — sab ek saath bhi de sakte ho
/cancel — connect flow beech me band karo
/disconnect — session band
/status — connection status
/screen — desktop ki photo

⌨️ Commands (slash aur plain text dono chalega):
/open chrome — app kholna (notepad, cmd, powershell, calc, mspaint...)
/open instagram — website kholna
/search instagram — Chrome me Google search
/goto instagram.com — URL kholna
/instagram — seedha Instagram
/install adblock — Chrome Web Store kholna
/cmd ipconfig — PowerShell me command chalana
/type hello — text likhna (Hindi bhi chalega)
/key ctrl+shift+esc — shortcut (win+r, alt+f4, win+l...)
/click 640 360 — mouse click (right/double bhi likh sakte ho)
/scroll down 3
/seq win+r; type chrome; press enter; wait 2; type instagram.com; press enter

Plain examples: "open chrome", "search instagram", "chrome kholo", "instagram kholo"
"""


def guarded(handler):
    """Sirf ALLOWED_USERS ke messages process kare (empty = sab allow, warning ke saath)."""

    @functools.wraps(handler)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id if update.effective_user else 0
        uname = update.effective_user.username if update.effective_user else None
        log.info("Message aaya: user id=%s username=%s chat=%s", uid, uname, update.effective_chat.id if update.effective_chat else None)
        if not cfg["allowed_users"]:
            log.warning("ALLOWED_USERS set nahi hai — sab users allow hain!")
        elif uid not in cfg["allowed_users"]:
            if update.message:
                await update.message.reply_text("⛔ Tum authorized nahi ho.")
            return
        return await handler(update, context)

    return wrapper


@guarded
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT)


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

    # Poora creds ek saath diya: seedha connect
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

    # Step-by-step flow shuru karo
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
        # Env se host already set hai — IP nahi puchna
        host, port = split_host_port(cfg["rdp_host"])
        session["host"] = host
        session["port"] = port
        session["step"] = "user"

    connect_sessions[chat_id] = session

    if session["step"] == "ip":
        await update.message.reply_text(
            "🔌 RDP connect — pehle IP address bhejo.\n"
            "Port 3389 hai to sirf IP likho (1.2.3.4).\n"
            "Alag port hai to `IP:port` likho — jaise `1.2.3.4:65430`.\n"
            "Band karne ke liye /cancel."
        )
    elif session["step"] == "user":
        await update.message.reply_text(f"🌐 IP: {session['host']}\n👤 Ab username bhejo:")
    else:
        await update.message.reply_text(
            f"🌐 IP: {session['host']}  👤 {session['user']}\n🔑 Ab password bhejo:"
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
        await update.message.reply_text(
            f"🌐 Host: {host}  Port: {port}\n👤 Ab username bhejo:"
        )
    elif step == "user":
        session["user"] = text
        session["step"] = "pass"
        await update.message.reply_text(
            "🔑 Ab password bhejo:\n"
            "⚠️ Password chat history me save ho jayega — connect ke baad ye message delete kar dena."
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
        await update.message.reply_text("❌ Connect flow cancel ho gaya.")
    else:
        await update.message.reply_text("Koi chalu connect flow nahi hai.")


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
        await update.message.reply_text(f"❌ Screenshot fail: {e}")


@guarded
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()

    # /connect ka step-by-step flow chal raha hai to answer yahin jayega
    session = connect_sessions.get(update.effective_chat.id)
    if session:
        return await _handle_connect_step(update, session)

    # Plain-text shortcuts for connection/screen commands
    low = text.lower()
    if low in ("screen", "screenshot", "photo", "desktop photo"):
        return await cmd_screen(update, context)
    if low in ("connect", "login"):
        return await cmd_connect(update, context)
    if low in ("disconnect", "logout"):
        return await cmd_disconnect(update, context)
    if low in ("status",):
        return await cmd_status(update, context)

    try:
        actions, desc = commands.parse_command(text)
    except ValueError as e:
        await update.message.reply_text(f"⚠️ {e}\n\nCommands dekho: /start")
        return

    try:
        await mgr.execute(actions)
        await update.message.reply_text(f"✅ {desc}")
    except Exception as e:
        log.exception("execute failed")
        await update.message.reply_text(f"❌ Error: {e}")


def start_health_server() -> None:
    """Chhota sa HTTP server — health check / free-tier keep-alive ping ke liye.

    Render free web services 15 min idle par sleep ho jate hain. UptimeRobot jaisi
    free service is URL ko har 5 min ping karti hai, jisse service jaagti rehti hai.
    """
    port = int(os.environ.get("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 (http.server API)
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):
            pass  # shanti rakho

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server chal raha hai :%s par", port)


def main() -> None:
    if not cfg["telegram_token"]:
        raise SystemExit("TELEGRAM_TOKEN missing — env me set karo")

    start_health_server()

    app = Application.builder().token(cfg["telegram_token"]).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(CommandHandler("connect", cmd_connect))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("disconnect", cmd_disconnect))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("screen", cmd_screen))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("Unhandled error: %s", context.error, exc_info=context.error)

    app.add_error_handler(on_error)
    log.info("Bot start ho raha hai...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
