"""Telegram commands for selecting Instagram inbox chats and controlling Automa.

Workflow JSON is syntax-checked but its settings and block types are preserved.
Uploading imports only; execution happens through /start.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import os
import re
from typing import Any

from PIL import Image
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from automa_guard import WorkflowValidationError, validate_automa_json
from automa_runtime import ChatFlow, GroupChoice, WorkflowVault, MAX_SELECTED_GROUPS

vault = WorkflowVault(ttl_seconds=600)
flows: dict[int, ChatFlow] = {}
_download_names: dict[str, str] = {}
INBOX_URL = "https://www.instagram.com/direct/inbox/"


def visible_chat_rows(image: Image.Image, limit: int = MAX_SELECTED_GROUPS) -> list[int]:
    """Find nonblank visible inbox rows by avatar pixels; returns zero-based indexes."""
    rgb = image.convert("RGB")
    width, height = rgb.size
    sx, sy = width / 1366.0, height / 768.0
    found: list[int] = []
    for i in range(limit):
        x0, y0 = int(116*sx), int((393 + 73*i)*sy)
        x1, y1 = int(165*sx), int((440 + 73*i)*sy)
        if y1 > height or x1 > width:
            break
        pixels = list(rgb.crop((x0, y0, x1, y1)).get_flattened_data())
        if not pixels:
            break
        saturated = sum(1 for r, g, b in pixels if max(r, g, b) - min(r, g, b) > 35 and max(r, g, b) > 80)
        dark = sum(1 for r, g, b in pixels if max(r, g, b) < 220)
        if saturated / len(pixels) < 0.035 and dark / len(pixels) < 0.22:
            break
        found.append(i)
    return found


def _chat_keyboard(flow: ChatFlow) -> InlineKeyboardMarkup:
    rows = []
    for choice in flow.choices:
        selected = choice.index in flow.selected
        label = ("✅ " if selected else "▫️ ") + choice.label[:48]
        rows.append([InlineKeyboardButton(label, callback_data=f"iggc:{choice.index}")])
    rows.append([InlineKeyboardButton("Done — /done", callback_data="iggc:done")])
    return InlineKeyboardMarkup(rows)


def _scale(mgr, x: int, y: int) -> tuple[int, int]:
    return int(x * mgr.client.width / 1366), int(y * mgr.client.height / 768)


def _target_keys(flow: ChatFlow) -> list[str]:
    digest = flow.workflow_digest or ""
    profile = flow.profile or "active"
    return [hashlib.sha256(f"{digest}:{profile}:{idx}".encode()).hexdigest() for idx in sorted(flow.selected)]


def _classify_run_control(image: Image.Image) -> str:
    """Conservatively distinguish the Automa play triangle from a solid stop square."""
    sx, sy = image.width / 1366.0, image.height / 768.0
    cx, cy = int(760*sx), int(335*sy)
    crop = image.convert("RGB").crop((cx-int(9*sx), cy-int(10*sy), cx+int(9*sx), cy+int(10*sy)))
    w, h = crop.size
    points = [(i % w, i // w) for i, (r, g, b) in enumerate(crop.get_flattened_data()) if max(r, g, b) < 100]
    if len(points) < 20:
        return "unknown"
    min_x, max_x = min(x for x, _ in points), max(x for x, _ in points)
    min_y, max_y = min(y for _, y in points), max(y for _, y in points)
    bw, bh = max_x-min_x+1, max_y-min_y+1
    density = len(points) / max(1, bw*bh)
    ratio = bw / max(1, bh)
    if 0.65 <= ratio <= 1.35 and density >= 0.72 and bw <= int(12*sx) and bh <= int(12*sy):
        return "stop"
    if 0.55 <= ratio <= 1.1 and 0.20 <= density <= 0.62 and 8*sy <= bh <= 18*sy:
        return "play"
    return "unknown"


async def _open_automa_dashboard(bot, mgr) -> None:
    """Open Automa through the pinned Edge extension icon, then Dashboard."""
    await mgr.ensure()
    if not await bot._focus_edge_window(mgr):
        raise RuntimeError("Edge window could not be focused; refusing dashboard clicks")
    x, y = _scale(mgr, 838, 67)
    hx, hy = _scale(mgr, 798, 136)
    await mgr.execute([("click", x, y, 1, False), ("wait", 0.8),
                       ("click", hx, hy, 1, False), ("wait", 3.0)])


async def cmd_fetchgc(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    if update.effective_chat.type != "private":
        await update.message.reply_text("/fetchgc sirf private Telegram chat mein use karo.")
        return
    try:
        mgr = bot.get_mgr()
        await mgr.ensure()
        res = await bot._open_url_for_profile(mgr, INBOX_URL, bot.current.get("host", ""))
        if not res or "Not found" in res:
            await update.message.reply_text("Instagram inbox open nahi hua. /screen se check karo.")
            return
        await asyncio.sleep(3)
        image = await mgr.screenshot()
        rows = visible_chat_rows(image)
        if not rows:
            await update.message.reply_text("No chats found.")
            return
        flow = flows.setdefault(update.effective_chat.id, ChatFlow())
        flow.set_choices([GroupChoice(i+1, f"Inbox chat {i+1}", 393+73*i) for i in rows])
        buf = io.BytesIO(); image.save(buf, format="PNG"); buf.seek(0)
        await update.message.reply_photo(
            photo=buf,
            caption=f"Pehle {len(rows)} visible inbox chats, screenshot ke top-to-bottom order mein. Sirf group chats select karo; /done se finalize karo. Koi message nahi bheja.",
            reply_markup=_chat_keyboard(flow),
        )
    except Exception as exc:
        bot.log.exception("/fetchgc failed")
        await update.message.reply_text(f"❌ Chats fetch nahi hue: {exc}")


async def on_chat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    query = update.callback_query
    await query.answer()
    flow = flows.get(update.effective_chat.id)
    if not flow:
        await query.edit_message_text("Chat list expire ho gayi. /fetchgc dobara chalao.")
        return
    if query.data == "iggc:done":
        await query.message.reply_text("Selection finalize karne ke liye /done bhejo.")
        return
    try:
        index = int(query.data.split(":", 1)[1])
        selected = flow.toggle(index)
    except (ValueError, IndexError):
        return
    await query.edit_message_reply_markup(reply_markup=_chat_keyboard(flow))
    await query.answer("Selected" if selected else "Removed")


async def cmd_done(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    flow = flows.get(update.effective_chat.id)
    if not flow:
        await update.message.reply_text("Pehle /fetchgc chalao.")
        return
    try:
        selected = flow.finish_selection()
    except ValueError as exc:
        await update.message.reply_text(str(exc))
        return
    try:
        mgr = bot.get_mgr(flow.profile)
        await mgr.ensure()
        for position, choice in enumerate(selected):
            if position:
                await mgr.execute([("key", "ctrl+l"), ("text", INBOX_URL), ("key", "alt+enter"), ("wait", 4.0)])
            x, y = _scale(mgr, 260, choice.row_y + 24)
            await mgr.execute([("click", x, y, 1, False), ("wait", 3.0)])
        flow.active_tabs = list(range(len(selected)))
        image = await mgr.screenshot()
        buf = io.BytesIO(); image.save(buf, format="PNG"); buf.seek(0)
        caption = (f"{len(selected)} selected chat tab(s) prepared. Workflow abhi nahi chala. Verify tabs/screens, then /start."
                   if flow.workflow_name else f"{len(selected)} selected chat tab(s) prepared. Ab /setautoma se workflow import karo; koi workflow run nahi hua.")
        await update.message.reply_photo(photo=buf, caption=caption)
    except Exception as exc:
        flow.active_tabs = []
        bot.log.exception("/done tab preparation failed")
        await update.message.reply_text(f"❌ Tabs prepare nahi hue: {exc}")


async def cmd_setautoma(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    if update.effective_chat.type != "private":
        await update.message.reply_text("/setautoma sirf private Telegram chat mein use karo.")
        return
    try:
        mgr = bot.get_mgr()
        await _open_automa_dashboard(bot, mgr)
        flow = flows.setdefault(update.effective_chat.id, ChatFlow())
        flow.profile = bot.mgr_key()
        context.user_data["automa_upload_pending"] = True
        await update.message.reply_text("Automa dashboard khol diya. Ab apna exported .json workflow isi private chat mein document bana kar bhejo. Workflow import hoga; upload par run nahi hoga.")
    except Exception as exc:
        bot.log.exception("/setautoma failed")
        await update.message.reply_text(f"❌ Automa dashboard nahi khula: {exc}")


def _base_url() -> str:
    return (os.environ.get("AUTOMA_DOWNLOAD_BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL") or "https://telegram-rdp-bot.onrender.com").rstrip("/")


def get_download(token: str):
    entry = vault.get(token)
    if entry is None:
        _download_names.pop(token, None)
        return None
    return entry, _download_names.get(token, entry.filename)


async def _run_flow_once_per_tab(flow: ChatFlow, bot) -> int:
    """Run the selected workflow once per prepared tab, only when Play is visible."""
    flow.completed_targets.update(_target_keys(flow))
    flow.run_state = "starting"
    flow.run_completed = 0
    mgr = bot.get_mgr(flow.profile)
    await mgr.ensure()
    for _ in flow.active_tabs:
        x, y = _scale(mgr, 838, 67)
        sx, sy = _scale(mgr, 650, 192)
        rx, ry = _scale(mgr, 760, 335)
        await mgr.execute([("click", x, y, 1, False), ("wait", 0.8),
                           ("click", sx, sy, 1, False), ("key", "ctrl+a"),
                           ("text", flow.workflow_name), ("wait", 0.8)])
        image = await mgr.screenshot()
        if _classify_run_control(image) != "play":
            raise RuntimeError("Automa play control not positively identified; stopped before clicking")
        await mgr.execute([("click", rx, ry, 1, False), ("wait", 1.0)])
        flow.run_completed += 1
        if flow.run_completed < len(flow.active_tabs):
            await mgr.execute([("key", "ctrl+shift+tab"), ("wait", 1.0)])
    flow.run_state = "running"
    return flow.run_completed


async def on_workflow_document(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    if not update.message or not update.message.document or update.effective_chat.type != "private":
        return
    if not context.user_data.get("automa_upload_pending"):
        await update.message.reply_text("Workflow receive karne se pehle /setautoma chalao.")
        return
    doc_file = update.message.document
    filename = doc_file.file_name or "workflow.json"
    if not filename.lower().endswith(".json") or (doc_file.file_size and doc_file.file_size > 20 * 1024 * 1024):
        await update.message.reply_text("Sirf 20 MB tak ki .json workflow file bhejo.")
        return
    try:
        tgfile = await context.bot.get_file(doc_file.file_id)
        raw = bytes(await tgfile.download_as_bytearray())
        parsed, summary = validate_automa_json(raw)
    except WorkflowValidationError as exc:
        await update.message.reply_text(f"Workflow reject hua: {exc}")
        return
    except Exception as exc:
        bot.log.exception("workflow document validation failed")
        await update.message.reply_text(f"❌ JSON read/validate nahi hua: {exc}")
        return
    # Preserve the uploaded workflow exactly: do not force background execution,
    # rewrite settings, or filter node/block types.
    flow = flows.setdefault(update.effective_chat.id, ChatFlow())
    digest = hashlib.sha256(raw).hexdigest()
    if flow.workflow_digest != digest:
        flow.run_state = None
        flow.run_completed = 0
    flow.workflow_digest = digest
    flow.workflow_name = summary.name
    flow.workflow_bytes = raw
    flow.send_clicks = summary.send_clicks
    flow.profile = flow.profile or bot.mgr_key()
    token = vault.put(filename, raw)
    safe_name = f"automa-{token[:8]}.json"
    _download_names[token] = safe_name
    try:
        mgr = bot.get_mgr(flow.profile)
        base = _base_url()
        # Dashboard is a separate window; switch to Edge, download the temporary
        # JSON, then return to Automa's import control.
        await mgr.execute([("key", "alt+tab"), ("wait", 0.5),
                           ("click", int(mgr.client.width*0.29), int(mgr.client.height*0.09), 1, False),
                           ("key", "ctrl+l"), ("text", f"{base}/automa-file/{token}"), ("key", "enter"), ("wait", 6.0)])
        await _open_automa_dashboard(bot, mgr)
        x, y = _scale(mgr, 124, 243)
        username = (mgr.cfg.get("rdp_user") or "testing").split("\\")[-1]
        username = re.sub(r"[^A-Za-z0-9._-]", "", username) or "testing"
        target = f"C:\\Users\\{username}\\Downloads\\{safe_name}"
        await mgr.execute([("click", x, y, 1, False), ("wait", 2.0), ("key", "alt+n"),
                           ("text", target), ("key", "enter"), ("wait", 7.0)])
        vault.discard(token); _download_names.pop(token, None)
        context.user_data["automa_upload_pending"] = False
        image = await mgr.screenshot()
        buf = io.BytesIO(); image.save(buf, format="PNG"); buf.seek(0)
        await update.message.reply_photo(photo=buf, caption=f"Import sequence finished for ‘{summary.name}’ ({summary.node_count} blocks detected). Original workflow settings and blocks preserved; it was not run. Verify it is listed in Automa.")
    except Exception as exc:
        vault.discard(token); _download_names.pop(token, None)
        bot.log.exception("Automa import UI failed")
        await update.message.reply_text(f"❌ Import verify nahi hua: {exc}. Workflow run nahi hua.")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    flow = flows.get(update.effective_chat.id)
    if not flow or not flow.confirmed or not flow.active_tabs:
        await update.message.reply_text("Pehle /fetchgc → groups select → /done se tabs ready karo.")
        return
    if not flow.workflow_name:
        await update.message.reply_text("Pehle /setautoma se workflow import karo.")
        return
    try:
        count = await _run_flow_once_per_tab(flow, bot)
        await update.message.reply_text(f"Run clicks bheje: ‘{flow.workflow_name}’ ek baar per selected tab ({count}). /stop se rukao; /screen se status verify karo.")
    except Exception as exc:
        flow.run_state = "partial"
        bot.log.exception("/start Automa run failed")
        await update.message.reply_text(f"❌ Run beech mein fail hua ({flow.run_completed}/{len(flow.active_tabs)} tabs attempted). Status /screen se verify karo. Error: {exc}")


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    flow = flows.get(update.effective_chat.id)
    if not flow or flow.run_state not in ("starting", "running", "partial"):
        await update.message.reply_text("Koi active Automa run tracked nahi hai.")
        return
    try:
        mgr = bot.get_mgr(flow.profile)
        clicked = 0
        for _ in flow.active_tabs:
            x, y = _scale(mgr, 838, 67)
            search_x, search_y = _scale(mgr, 650, 192)
            rx, ry = _scale(mgr, 760, 335)
            await mgr.execute([("click", x, y, 1, False), ("wait", 0.8),
                               ("click", search_x, search_y, 1, False), ("key", "ctrl+a"),
                               ("text", flow.workflow_name or ""), ("wait", 0.8)])
            image = await mgr.screenshot()
            if _classify_run_control(image) == "stop":
                await mgr.execute([("click", rx, ry, 1, False), ("wait", 1.0)])
                clicked += 1
            await mgr.execute([("key", "ctrl+shift+tab")])
        flow.run_state = "stopped" if clicked else "finished-or-not-detected"
        await update.message.reply_text(f"Stop click {clicked}/{len(flow.active_tabs)} tab(s) par bheja. Agar stop icon nahi dikha to click nahi kiya—/screen se status verify karo.")
    except Exception as exc:
        bot.log.exception("/stop failed")
        await update.message.reply_text(f"❌ Stop verify nahi hua: {exc}")


async def on_automa_control(update: Update, context: ContextTypes.DEFAULT_TYPE, bot) -> None:
    """Reject stale confirmation buttons from earlier versions of this bot."""
    query = update.callback_query
    await query.answer()
    if query.data == "automa:run-cancel":
        await query.edit_message_text("Run cancel kiya; workflow nahi chala.")
    else:
        await query.edit_message_text("Purana confirmation button expire ho gaya. Agar intended ho to /start bhejo.")


def register(app, bot) -> None:
    """Register feature handlers on the existing Telegram application."""
    from telegram.ext import CallbackQueryHandler, CommandHandler, MessageHandler, filters
    app.add_handler(CommandHandler("fetchgc", bot.guarded(lambda u, c: cmd_fetchgc(u, c, bot))))
    app.add_handler(CommandHandler("done", bot.guarded(lambda u, c: cmd_done(u, c, bot))))
    app.add_handler(CommandHandler("setautoma", bot.guarded(lambda u, c: cmd_setautoma(u, c, bot))))
    app.add_handler(CommandHandler("start", bot.guarded(lambda u, c: cmd_start(u, c, bot))))
    app.add_handler(CommandHandler("stop", bot.guarded(lambda u, c: cmd_stop(u, c, bot))))
    app.add_handler(CallbackQueryHandler(bot.guarded(lambda u, c: on_chat_callback(u, c, bot)), pattern=r"^iggc:"))
    app.add_handler(CallbackQueryHandler(bot.guarded(lambda u, c: on_automa_control(u, c, bot)), pattern=r"^automa:"))
    app.add_handler(MessageHandler(filters.Document.ALL, bot.guarded(lambda u, c: on_workflow_document(u, c, bot))))
