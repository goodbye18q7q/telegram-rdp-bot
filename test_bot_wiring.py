"""bot.py ke handler wiring ke smoke tests (Telegram/RDP dono mock).

Chalane ke liye:  python -m unittest test_bot_wiring -v
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import bot


def make_update(text: str, uid: int = 111, chat_id: int = 555):
    msg = MagicMock()
    msg.text = text
    msg.reply_text = AsyncMock()
    msg.reply_photo = AsyncMock()
    update = MagicMock()
    update.message = msg
    update.effective_user.id = uid
    update.effective_chat.id = chat_id
    return update, msg


class TestWiring(unittest.TestCase):
    def setUp(self):
        bot.mgr.execute = AsyncMock()
        bot.mgr.connect = AsyncMock()
        bot.mgr.screenshot = AsyncMock()
        bot.connect_sessions.clear()
        bot.cfg["allowed_users"] = []
        bot.cfg["rdp_host"] = ""
        bot.cfg["rdp_user"] = ""
        bot.cfg["rdp_pass"] = ""

    def test_open_chrome(self):
        update, msg = make_update("open chrome")
        asyncio.run(bot.on_message(update, MagicMock()))
        bot.mgr.execute.assert_awaited_once()
        actions = bot.mgr.execute.await_args[0][0]
        self.assertIn(("key", "win+r"), actions)
        self.assertIn(("text", "chrome"), actions)
        msg.reply_text.assert_awaited_once()

    def test_plain_screen_shortcut(self):
        update, msg = make_update("screen")
        asyncio.run(bot.on_message(update, MagicMock()))
        bot.mgr.screenshot.assert_awaited_once()

    def test_unauthorized(self):
        bot.cfg["allowed_users"] = [999]
        update, msg = make_update("open chrome", uid=111)
        asyncio.run(bot.on_message(update, MagicMock()))
        bot.mgr.execute.assert_not_awaited()
        msg.reply_text.assert_awaited_once()
        self.assertIn("authorized", msg.reply_text.await_args[0][0])

    def test_unknown_command(self):
        update, msg = make_update("asdfghjkl")
        asyncio.run(bot.on_message(update, MagicMock()))
        bot.mgr.execute.assert_not_awaited()
        self.assertIn("Unknown", msg.reply_text.await_args[0][0])

    def test_rdp_error_reported(self):
        bot.mgr.execute = AsyncMock(side_effect=ConnectionError("RDP_HOST missing"))
        update, msg = make_update("/cmd ipconfig")
        asyncio.run(bot.on_message(update, MagicMock()))
        self.assertIn("Error", msg.reply_text.await_args[0][0])


class TestConnectFlow(unittest.TestCase):
    def setUp(self):
        bot.mgr.connect = AsyncMock()
        bot.connect_sessions.clear()
        bot.cfg["allowed_users"] = []
        bot.cfg["rdp_host"] = ""
        bot.cfg["rdp_user"] = ""
        bot.cfg["rdp_pass"] = ""

    def test_step_by_step_ip_user_pass(self):
        chat_id = 555

        # 1) /connect -> IP poochta hai
        update, _ = make_update("/connect", chat_id=chat_id)
        ctx = MagicMock()
        ctx.args = []
        asyncio.run(bot.cmd_connect(update, ctx))
        self.assertEqual(bot.connect_sessions[chat_id]["step"], "ip")

        # 2) IP -> username poochta hai
        u2, _ = make_update("1.2.3.4", chat_id=chat_id)
        asyncio.run(bot.on_message(u2, MagicMock()))
        self.assertEqual(bot.connect_sessions[chat_id]["step"], "user")

        # 3) username -> password poochta hai
        u3, _ = make_update("Administrator", chat_id=chat_id)
        asyncio.run(bot.on_message(u3, MagicMock()))
        self.assertEqual(bot.connect_sessions[chat_id]["step"], "pass")

        # 4) password -> connect
        u4, _ = make_update("secret123", chat_id=chat_id)
        asyncio.run(bot.on_message(u4, MagicMock()))
        bot.mgr.connect.assert_awaited_once()
        self.assertNotIn(chat_id, bot.connect_sessions)
        self.assertEqual(bot.cfg["rdp_host"], "1.2.3.4")
        self.assertEqual(bot.cfg["rdp_user"], "Administrator")
        self.assertEqual(bot.cfg["rdp_pass"], "secret123")

    def test_flow_asks_only_missing_steps(self):
        # /connect ip user -> sirf password poochta hai
        update, _ = make_update("/connect 9.9.9.9 bob")
        ctx = MagicMock()
        ctx.args = ["9.9.9.9", "bob"]
        asyncio.run(bot.cmd_connect(update, ctx))
        self.assertEqual(bot.connect_sessions[555]["step"], "pass")

    def test_flow_uses_env_host(self):
        bot.cfg["rdp_host"] = "env-host"
        update, _ = make_update("/connect")
        ctx = MagicMock()
        ctx.args = []
        asyncio.run(bot.cmd_connect(update, ctx))
        self.assertEqual(bot.connect_sessions[555]["step"], "user")
        self.assertEqual(bot.connect_sessions[555]["host"], "env-host")

    def test_split_host_port(self):
        self.assertEqual(bot.split_host_port("1.2.3.4"), ("1.2.3.4", 3389))
        self.assertEqual(bot.split_host_port("1.2.3.4:65430"), ("1.2.3.4", 65430))
        self.assertEqual(bot.split_host_port(" 103.76.214.2:65430 "), ("103.76.214.2", 65430))
        self.assertEqual(bot.split_host_port("[2001:db8::1]:3389"), ("2001:db8::1", 3389))

    def test_flow_parses_ip_port(self):
        # User ne "IP:port" poora bhara tha — ab sahi parse hona chahiye
        update, _ = make_update("/connect")
        ctx = MagicMock()
        ctx.args = []
        asyncio.run(bot.cmd_connect(update, ctx))

        u2, _ = make_update("103.76.214.2:65430")
        asyncio.run(bot.on_message(u2, MagicMock()))
        self.assertEqual(bot.connect_sessions[555]["host"], "103.76.214.2")
        self.assertEqual(bot.connect_sessions[555]["port"], 65430)

        u3, _ = make_update("Administrator")
        asyncio.run(bot.on_message(u3, MagicMock()))
        u4, _ = make_update("pw123")
        asyncio.run(bot.on_message(u4, MagicMock()))
        bot.mgr.connect.assert_awaited_once()
        self.assertEqual(bot.cfg["rdp_host"], "103.76.214.2")
        self.assertEqual(bot.cfg["rdp_port"], 65430)

    def test_cancel(self):
        update, _ = make_update("/connect")
        ctx = MagicMock()
        ctx.args = []
        asyncio.run(bot.cmd_connect(update, ctx))
        self.assertIn(555, bot.connect_sessions)

        u2, m2 = make_update("/cancel")
        asyncio.run(bot.cmd_cancel(u2, MagicMock()))
        self.assertNotIn(555, bot.connect_sessions)
        bot.mgr.connect.assert_not_awaited()

    def test_one_shot_args(self):
        update, _ = make_update("/connect 8.8.8.8 admin pw123")
        ctx = MagicMock()
        ctx.args = ["8.8.8.8", "admin", "pw123"]
        asyncio.run(bot.cmd_connect(update, ctx))
        bot.mgr.connect.assert_awaited_once()
        self.assertEqual(bot.cfg["rdp_host"], "8.8.8.8")
        self.assertNotIn(555, bot.connect_sessions)


class TestWinCtrl(unittest.TestCase):
    def setUp(self):
        bot.mgr.execute = AsyncMock()
        bot.mgr.connect = AsyncMock()
        bot.mgr.screenshot = AsyncMock()
        bot.connect_sessions.clear()
        bot.cfg["allowed_users"] = []

    @staticmethod
    def img(color):
        from PIL import Image

        return Image.new("RGB", (120, 80), color)

    def test_win_no_word(self):
        update, msg = make_update("/win")
        asyncio.run(bot.cmd_win(update, MagicMock()))
        self.assertIn("Not found", msg.reply_text.await_args[0][0])
        bot.mgr.execute.assert_not_awaited()

    def test_win_opened(self):
        update, msg = make_update("/win chrome")
        ctx = MagicMock()
        ctx.args = ["chrome"]
        bot.mgr.screenshot = AsyncMock(side_effect=[self.img((0, 0, 0)), self.img((255, 255, 255))])
        asyncio.run(bot.cmd_win(update, ctx))
        self.assertIn("Opened", msg.reply_text.await_args[0][0])

    def test_win_not_found(self):
        update, msg = make_update("/win zzznotanapp")
        ctx = MagicMock()
        ctx.args = ["zzznotanapp"]
        same = self.img((10, 10, 10))
        bot.mgr.screenshot = AsyncMock(side_effect=[same, same])
        asyncio.run(bot.cmd_win(update, ctx))
        self.assertIn("Not found", msg.reply_text.await_args[0][0])

    def test_ctrl(self):
        update, msg = make_update("/ctrl hello")
        ctx = MagicMock()
        ctx.args = ["hello"]
        asyncio.run(bot.cmd_ctrl(update, ctx))
        self.assertIn("Typed", msg.reply_text.await_args[0][0])
        actions = bot.mgr.execute.await_args[0][0]
        self.assertEqual(actions[0], ("key", "ctrl+a"))
        self.assertEqual(actions[2], ("text", "hello"))

    def test_diff_ratio(self):
        self.assertEqual(bot.diff_ratio(self.img((0, 0, 0)), self.img((0, 0, 0))), 0.0)
        self.assertGreater(bot.diff_ratio(self.img((0, 0, 0)), self.img((255, 255, 255))), 0.9)


if __name__ == "__main__":
    unittest.main()
