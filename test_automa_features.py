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

    @staticmethod
    def dashboard_image() -> Image.Image:
        image = Image.new("RGB", (1366, 768), "white")
        draw = ImageDraw.Draw(image)
        for y in (125, 175, 225, 275, 325, 370):
            draw.rectangle((22, y-5, 39, y+5), fill="black")
        return image

    @staticmethod
    def popup_image(row_y: int = 335, control: str = "play") -> Image.Image:
        image = Image.new("RGB", (1366, 768), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((772, 84, 1122, 277), fill=(20, 20, 20))
        if control == "play":
            draw.line([(1040,row_y-6),(1049,row_y),(1040,row_y+6),(1040,row_y-6)], fill="black", width=2)
        elif control == "stop":
            draw.rectangle((1041,row_y-3,1047,row_y+3), fill="black")
        return image

    def setUp(self):
        self.bot = SimpleNamespace(_focus_edge_window=AsyncMock(return_value=True))
        self.chat_id = 99
        automa_features.flows.pop(self.chat_id, None)

    def tearDown(self):
        automa_features.flows.pop(self.chat_id, None)

    def test_start_runs_without_extra_prompt_once_per_tab(self):
        play_img = self.popup_image()
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
        self.assertEqual(mgr.screenshot.await_count, 4)

    def test_start_runs_selected_saved_workflow_row(self):
        index, row_y = 2, 335 + 80*2
        play_img = self.popup_image(row_y)
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
        self.assertIn(("click", 1044, row_y, 1, False), clicks)
        self.assertIn("Saved workflow 3", message.reply_text.await_args.args[0])

    def test_auto_refresh_reloads_only_the_focused_tab_without_focus_probe(self):
        async def run():
            flow = ChatFlow(run_state="running", active_tabs=[0, 1], profile="rdp1")
            mgr = SimpleNamespace(ensure=AsyncMock(), execute=AsyncMock())
            bot = SimpleNamespace(get_mgr=MagicMock(return_value=mgr),
                                  _focus_edge_window=AsyncMock(return_value=True),
                                  log=MagicMock())
            telegram_bot = SimpleNamespace(send_message=AsyncMock(side_effect=lambda **kwargs:
                setattr(flow, "run_state", "stopped")))
            with unittest.mock.patch.object(automa_features, "AUTO_REFRESH_SECONDS", 0.005):
                await automa_features._auto_refresh_tabs(self.chat_id, flow, bot, telegram_bot)
            actions = [action for call in mgr.execute.await_args_list for action in call.args[0]]
            self.assertEqual(sum(action == ("key", "ctrl+r") for action in actions), 1)
            bot._focus_edge_window.assert_not_awaited()
            self.assertIn("may interrupt", telegram_bot.send_message.await_args.kwargs["text"])

        asyncio.run(run())

    def test_start_requires_selected_groups_and_prepared_tabs(self):
        automa_features.flows[self.chat_id] = ChatFlow(workflow_name="Safe test")
        update, message = update_for_message(self.chat_id)
        asyncio.run(automa_features.cmd_start(update, MagicMock(), self.bot))
        self.assertIn("/fetchgc", message.reply_text.await_args.args[0])

    def test_repeat_start_is_not_blocked_by_previous_run_state(self):
        play_img = self.popup_image()
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
        image = self.popup_image()
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
        ImageDraw.Draw(play_img).line([(1040,329),(1049,335),(1040,341),(1040,329)], fill="black", width=2)
        stop_img = Image.new("RGB", (1366, 768), "white")
        ImageDraw.Draw(stop_img).rectangle((1041,332,1047,338), fill="black")
        self.assertEqual(automa_features._classify_run_control(play_img), "play")
        self.assertEqual(automa_features._classify_run_control(stop_img), "stop")

    def test_automa_dashboard_detection_skips_extension_clicks_when_already_open(self):
        mgr = SimpleNamespace(client=SimpleNamespace(width=1366, height=768), ensure=AsyncMock(),
                              screenshot=AsyncMock(return_value=self.dashboard_image()),
                              execute=AsyncMock())
        bot = SimpleNamespace(_focus_edge_window=AsyncMock(return_value=True))
        asyncio.run(automa_features._open_automa_dashboard(bot, mgr))
        mgr.execute.assert_not_awaited()
        bot._focus_edge_window.assert_not_awaited()

    def test_dashboard_detector_rejects_blank_white_page(self):
        self.assertFalse(automa_features._automa_dashboard_visible(Image.new("RGB", (1366,768), "white")))
        self.assertTrue(automa_features._automa_dashboard_visible(self.dashboard_image()))
        from bot import _looks_like_edge_window
        self.assertFalse(_looks_like_edge_window(self.dashboard_image()))

    def test_automa_popup_failure_stops_before_dashboard_page_click(self):
        mgr = SimpleNamespace(client=SimpleNamespace(width=1366, height=768), ensure=AsyncMock(),
                              screenshot=AsyncMock(return_value=Image.new("RGB", (1366,768), "white")),
                              execute=AsyncMock())
        bot = SimpleNamespace(_focus_edge_window=AsyncMock(return_value=True))
        with self.assertRaisesRegex(RuntimeError, "popup was not detected"):
            asyncio.run(automa_features._open_automa_popup(bot, mgr))
        clicks = [a for call in mgr.execute.await_args_list for a in call.args[0] if a[0] == "click"]
        self.assertEqual(clicks, [("click", 1122, 67, 1, False)])

    def test_edge_focus_retries_taskbar_restore_and_verifies_chrome(self):
        from bot import _focus_edge_window
        dark = Image.new("RGB", (1366, 768), (20, 80, 130))
        edge = Image.new("RGB", (1366, 768), "white")
        ImageDraw.Draw(edge).rectangle((110,55,910,67), fill="black")
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
