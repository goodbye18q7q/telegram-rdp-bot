from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from PIL import Image, ImageDraw
import automa_features
from automa_runtime import ChatFlow


def update_for_message(chat_id: int = 99):
    message = MagicMock()
    message.reply_text = AsyncMock()
    message.reply_photo = AsyncMock()
    update = MagicMock()
    update.message = message
    update.effective_chat.id = chat_id
    update.effective_chat.type = "private"
    return update, message


class TestAutomaFeatureControls(unittest.TestCase):
    @staticmethod
    def inbox_image(rows: int) -> Image.Image:
        image = Image.new("RGB", (1366, 768), "white")
        draw = ImageDraw.Draw(image)
        for i in range(rows):
            y = 393 + 73*i
            draw.ellipse((118, y, 162, y+47), fill=(190, 85 + i*20, 120))
        return image

    def setUp(self):
        self.bot = SimpleNamespace(_focus_edge_window=AsyncMock(return_value=True))
        self.chat_id = 99
        automa_features.flows.pop(self.chat_id, None)

    def tearDown(self):
        automa_features.flows.pop(self.chat_id, None)

    def test_start_runs_without_extra_prompt_once_per_tab(self):
        play_img = Image.new("RGB", (1366, 768), "white")
        ImageDraw.Draw(play_img).line([(756,329),(765,335),(756,341),(756,329)], fill="black", width=2)
        flow = ChatFlow(confirmed=True, selected={1}, workflow_digest="digest",
                        workflow_name="Safe test", active_tabs=[0, 1], send_clicks=1)
        automa_features.flows[self.chat_id] = flow
        mgr = SimpleNamespace(client=SimpleNamespace(width=1366, height=768),
                              ensure=AsyncMock(), execute=AsyncMock(), screenshot=AsyncMock(return_value=play_img))
        self.bot.get_mgr = MagicMock(return_value=mgr)
        self.bot.log = MagicMock()
        update, message = update_for_message(self.chat_id)
        asyncio.run(automa_features.cmd_start(update, MagicMock(), self.bot))
        message.reply_text.assert_awaited_once()
        self.assertNotIn("reply_markup", message.reply_text.await_args.kwargs)
        self.assertEqual(flow.run_state, "running")
        self.assertEqual(flow.run_completed, 2)
        self.assertEqual(flow.completed_targets, set(automa_features._target_keys(flow)))
        self.assertEqual(mgr.screenshot.await_count, 2)

    def test_start_runs_selected_saved_workflow_row(self):
        index, row_y = 2, 335 + 80*2
        play_img = Image.new("RGB", (1366, 768), "white")
        ImageDraw.Draw(play_img).line([(756,row_y-6),(765,row_y),(756,row_y+6),(756,row_y-6)], fill="black", width=2)
        flow = ChatFlow(confirmed=True, selected={1}, saved_workflow_index=index, active_tabs=[0])
        automa_features.flows[self.chat_id] = flow
        mgr = SimpleNamespace(client=SimpleNamespace(width=1366, height=768), ensure=AsyncMock(),
                              execute=AsyncMock(), screenshot=AsyncMock(return_value=play_img))
        self.bot.get_mgr = MagicMock(return_value=mgr)
        self.bot.mgr_key = lambda: "rdp1"
        self.bot.log = MagicMock()
        update, message = update_for_message(self.chat_id)
        asyncio.run(automa_features.cmd_start(update, MagicMock(), self.bot))
        clicks = [a for call in mgr.execute.await_args_list for a in call.args[0] if a[0] == "click"]
        self.assertIn(("click", 760, row_y, 1, False), clicks)
        self.assertIn("Saved workflow 3", message.reply_text.await_args.args[0])

    def test_start_requires_selected_groups_and_prepared_tabs(self):
        automa_features.flows[self.chat_id] = ChatFlow(workflow_name="Safe test")
        update, message = update_for_message(self.chat_id)
        asyncio.run(automa_features.cmd_start(update, MagicMock(), self.bot))
        self.assertIn("/fetchgc", message.reply_text.await_args.args[0])

    def test_repeat_start_is_not_blocked_by_previous_run_state(self):
        play_img = Image.new("RGB", (1366, 768), "white")
        ImageDraw.Draw(play_img).line([(756,329),(765,335),(756,341),(756,329)], fill="black", width=2)
        flow = ChatFlow(confirmed=True, selected={1}, workflow_digest="digest",
                        workflow_name="Repeatable workflow", active_tabs=[0], run_state="running")
        automa_features.flows[self.chat_id] = flow
        mgr = SimpleNamespace(client=SimpleNamespace(width=1366, height=768),
                              ensure=AsyncMock(), execute=AsyncMock(), screenshot=AsyncMock(return_value=play_img))
        self.bot.get_mgr = MagicMock(return_value=mgr)
        self.bot.log = MagicMock()
        update, message = update_for_message(self.chat_id)
        asyncio.run(automa_features.cmd_start(update, MagicMock(), self.bot))
        self.assertIn("Repeatable workflow", message.reply_text.await_args.args[0])
        self.assertEqual(flow.run_completed, 1)

    def test_workflow_command_shows_screenshot_and_five_choices(self):
        image = Image.new("RGB", (1366, 768), "white")
        mgr = SimpleNamespace(client=SimpleNamespace(width=1366, height=768), ensure=AsyncMock(),
                              execute=AsyncMock(), screenshot=AsyncMock(return_value=image))
        bot = SimpleNamespace(get_mgr=lambda: mgr, _focus_edge_window=AsyncMock(return_value=True),
                              mgr_key=lambda: "rdp1", log=MagicMock())
        update, message = update_for_message(self.chat_id)
        asyncio.run(automa_features.cmd_workflow(update, MagicMock(), bot))
        message.reply_photo.assert_awaited_once()
        keyboard = message.reply_photo.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(len(keyboard), 5)
        self.assertEqual(keyboard[4][0].callback_data, "automa:workflow:4")

    def test_saved_workflow_selection_is_recorded_without_running(self):
        from types import SimpleNamespace
        flow = ChatFlow()
        automa_features.flows[self.chat_id] = flow
        query = SimpleNamespace(data="automa:workflow:2", answer=AsyncMock(),
                                 edit_message_caption=AsyncMock())
        update = SimpleNamespace(callback_query=query, effective_chat=SimpleNamespace(id=self.chat_id))
        context = SimpleNamespace(user_data={})
        asyncio.run(automa_features.on_automa_control(update, context, self.bot))
        self.assertEqual(flow.saved_workflow_index, 2)
        self.assertEqual(flow.workflow_name, None)
        query.edit_message_caption.assert_awaited_once()

    def test_register_wires_requested_commands(self):
        from telegram.ext import CommandHandler
        app = MagicMock()
        bot = SimpleNamespace(guarded=lambda handler: handler)
        automa_features.register(app, bot)
        commands = [next(iter(call.args[0].commands)) for call in app.add_handler.call_args_list
                    if isinstance(call.args[0], CommandHandler)]
        self.assertEqual(set(commands), {"fetchgc", "done", "workflow", "setautoma", "start", "stop"})

    def test_run_button_classifier_distinguishes_play_and_stop(self):
        play_img = Image.new("RGB", (1366, 768), "white")
        ImageDraw.Draw(play_img).line([(756,329),(765,335),(756,341),(756,329)], fill="black", width=2)
        stop_img = Image.new("RGB", (1366, 768), "white")
        ImageDraw.Draw(stop_img).rectangle((757,332,763,338), fill="black")
        self.assertEqual(automa_features._classify_run_control(play_img), "play")
        self.assertEqual(automa_features._classify_run_control(stop_img), "stop")

    def test_edge_focus_retries_taskbar_restore_and_verifies_chrome(self):
        from bot import _focus_edge_window
        dark = Image.new("RGB", (1366, 768), (20, 80, 130))
        edge = Image.new("RGB", (1366, 768), "white")
        mgr = SimpleNamespace(client=SimpleNamespace(width=1366, height=768),
                              execute=AsyncMock(), screenshot_nowait=AsyncMock(side_effect=[dark, edge]))
        self.assertTrue(asyncio.run(_focus_edge_window(mgr)))
        self.assertEqual(mgr.execute.await_count, 2)

    def test_workflow_settings_are_preserved_in_uploaded_json(self):
        import json
        from automa_guard import validate_automa_json
        source = {"name": "Test", "settings": {"execContext": "popup", "special": True},
                  "drawflow": {"nodes": [{"label": "loop-data", "data": {"type": "loop-data"}}]}}
        doc, summary = validate_automa_json(json.dumps(source).encode())
        self.assertEqual(doc["settings"], source["settings"])
        self.assertEqual(summary.node_count, 1)

    def test_visible_rows_only_returns_nonblank_avatar_slots(self):
        image = self.inbox_image(2)
        self.assertEqual(automa_features.visible_chat_rows(image), [0, 1])
        self.assertEqual(automa_features.visible_chat_rows(Image.new("RGB", (1366, 768), "white")), [])

    def test_fetchgc_offers_buttons_for_detected_rows(self):
        mgr = SimpleNamespace(ensure=AsyncMock(), screenshot=AsyncMock(return_value=self.inbox_image(2)))
        bot = SimpleNamespace(get_mgr=lambda: mgr, _open_url_for_profile=AsyncMock(return_value="ok"),
                              current={"host":""}, log=MagicMock())
        update, message = update_for_message(self.chat_id)
        asyncio.run(automa_features.cmd_fetchgc(update, MagicMock(), bot))
        message.reply_photo.assert_awaited_once()
        markup = message.reply_photo.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "iggc:1")
        self.assertEqual(markup.inline_keyboard[1][0].callback_data, "iggc:2")

    def test_fetchgc_reports_no_chats_when_inbox_empty(self):
        mgr = SimpleNamespace(ensure=AsyncMock(), screenshot=AsyncMock(return_value=Image.new("RGB", (1366, 768), "white")))
        bot = SimpleNamespace(get_mgr=lambda: mgr, _open_url_for_profile=AsyncMock(return_value="ok"),
                              current={"host":""}, log=MagicMock())
        update, message = update_for_message(self.chat_id)
        asyncio.run(automa_features.cmd_fetchgc(update, MagicMock(), bot))
        self.assertEqual(message.reply_text.await_args.args[0], "No chats found.")


if __name__ == "__main__":
    unittest.main()
