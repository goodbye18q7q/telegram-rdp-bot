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


def fake_manager() -> MagicMock:
    m = MagicMock()
    m.execute = AsyncMock()
    m.connect = AsyncMock()
    m.disconnect = AsyncMock()
    m.screenshot = AsyncMock()
    m.is_connected = True
    return m


class BotTestCase(unittest.TestCase):
    def setUp(self):
        self._real_get_mgr = bot.get_mgr
        self._real_save = bot.save_profiles
        bot.save_profiles = lambda: None  # tests disk par likhe na
        bot.profiles.clear()
        bot.managers.clear()
        bot.connect_sessions.clear()
        bot.current.update(name=None, host="", port=3389, user="", password="")
        bot.cfg["allowed_users"] = []

    def tearDown(self):
        bot.get_mgr = self._real_get_mgr
        bot.save_profiles = self._real_save

    def use_fake(self) -> MagicMock:
        fake = fake_manager()
        bot.get_mgr = lambda name=None: fake
        return fake

    def save_profile(self, name="rdp1", host="1.2.3.4", port=3389, user="admin", pw="pw"):
        bot.profiles[name] = {"host": host, "port": port, "user": user, "password": pw}
        bot.current.update(name=name, host=host, port=port, user=user, password=pw)


class TestWiring(BotTestCase):
    def test_all_handlers_defined(self):
        # main() jo handlers register karta hai, sab exist karne chahiye
        for name in (
            "cmd_start", "cmd_add", "cmd_remove", "cmd_list", "cmd_switch", "cmd_connect",
            "cmd_cancel", "cmd_disconnect", "cmd_status", "cmd_screen", "cmd_win", "cmd_ctrl",
            "on_message",
        ):
            self.assertTrue(hasattr(bot, name), f"missing handler: {name}")

    def test_open_chrome(self):
        fake = self.use_fake()
        update, msg = make_update("open chrome")
        asyncio.run(bot.on_message(update, MagicMock()))
        fake.execute.assert_awaited_once()
        actions = fake.execute.await_args[0][0]
        self.assertIn(("key", "win+r"), actions)
        self.assertIn(("text", "chrome"), actions)
        msg.reply_text.assert_awaited_once()

    def test_plain_screen_shortcut(self):
        fake = self.use_fake()
        update, msg = make_update("screen")
        asyncio.run(bot.on_message(update, MagicMock()))
        fake.screenshot.assert_awaited_once()

    def test_unauthorized(self):
        self.use_fake()
        bot.cfg["allowed_users"] = [999]
        update, msg = make_update("open chrome", uid=111)
        asyncio.run(bot.on_message(update, MagicMock()))
        msg.reply_text.assert_awaited_once()
        self.assertIn("authorized", msg.reply_text.await_args[0][0])

    def test_unknown_command(self):
        self.use_fake()
        update, msg = make_update("asdfghjkl")
        asyncio.run(bot.on_message(update, MagicMock()))
        self.assertIn("Unknown", msg.reply_text.await_args[0][0])

    def test_rdp_error_reported(self):
        fake = self.use_fake()
        fake.execute = AsyncMock(side_effect=ConnectionError("no RDP selected"))
        update, msg = make_update("/cmd ipconfig")
        asyncio.run(bot.on_message(update, MagicMock()))
        self.assertIn("Error", msg.reply_text.await_args[0][0])


class TestProfiles(BotTestCase):
    def test_add_and_list(self):
        update, msg = make_update("/add RDP1 1.2.3.4:65430 admin pw123")
        ctx = MagicMock()
        ctx.args = ["RDP1", "1.2.3.4:65430", "admin", "pw123"]
        asyncio.run(bot.cmd_add(update, ctx))
        self.assertIn("rdp1", bot.profiles)
        self.assertEqual(bot.profiles["rdp1"]["host"], "1.2.3.4")
        self.assertEqual(bot.profiles["rdp1"]["port"], 65430)

        u2, m2 = make_update("/list")
        asyncio.run(bot.cmd_list(u2, MagicMock()))
        self.assertIn("rdp1", m2.reply_text.await_args[0][0])

    def test_switch_by_slash_name(self):
        self.save_profile()
        fake = self.use_fake()
        update, msg = make_update("/rdp2")  # saved profile "rdp1" hai, rdp2 nahi
        asyncio.run(bot.on_message(update, MagicMock()))
        self.assertIn("not found", msg.reply_text.await_args[0][0].lower())

    def test_switch_plain_rdp1(self):
        self.save_profile(name="rdp1", host="10.0.0.1")
        fake = self.use_fake()
        update, msg = make_update("rdp1")
        asyncio.run(bot.on_message(update, MagicMock()))
        fake.connect.assert_awaited_once()
        self.assertEqual(bot.current["name"], "rdp1")
        self.assertIn("Connected", msg.reply_text.await_args[0][0])

    def test_switch_plain_rdp_2(self):
        self.save_profile(name="rdp2", host="10.0.0.2")
        fake = self.use_fake()
        update, msg = make_update("rdp 2")
        asyncio.run(bot.on_message(update, MagicMock()))
        fake.connect.assert_awaited_once()
        self.assertEqual(bot.current["name"], "rdp2")

    def test_connect_saved_profile(self):
        self.save_profile(name="rdp1", host="10.0.0.1")
        fake = self.use_fake()
        update, msg = make_update("/connect rdp1")
        ctx = MagicMock()
        ctx.args = ["rdp1"]
        asyncio.run(bot.cmd_connect(update, ctx))
        fake.connect.assert_awaited_once()
        self.assertEqual(bot.current["name"], "rdp1")

    def test_one_shot_saves_profile(self):
        fake = self.use_fake()
        update, msg = make_update("/connect 8.8.8.8 admin pw")
        ctx = MagicMock()
        ctx.args = ["8.8.8.8", "admin", "pw"]
        asyncio.run(bot.cmd_connect(update, ctx))
        fake.connect.assert_awaited_once()
        self.assertIn("rdp1", bot.profiles)
        self.assertEqual(bot.current["host"], "8.8.8.8")

    def test_remove(self):
        self.save_profile()
        update, msg = make_update("/remove rdp1")
        ctx = MagicMock()
        ctx.args = ["rdp1"]
        asyncio.run(bot.cmd_remove(update, ctx))
        self.assertNotIn("rdp1", bot.profiles)

    def test_status(self):
        self.save_profile()
        self.use_fake()
        update, msg = make_update("/status")
        asyncio.run(bot.cmd_status(update, MagicMock()))
        text = msg.reply_text.await_args[0][0]
        self.assertIn("rdp1", text)
        self.assertIn("1.2.3.4", text)


class TestConnectFlow(BotTestCase):
    def test_step_by_step_saves_profile(self):
        fake = self.use_fake()
        chat_id = 555

        update, _ = make_update("/connect", chat_id=chat_id)
        ctx = MagicMock()
        ctx.args = []
        asyncio.run(bot.cmd_connect(update, ctx))
        self.assertEqual(bot.connect_sessions[chat_id]["step"], "ip")

        u2, _ = make_update("103.76.214.2:65430", chat_id=chat_id)
        asyncio.run(bot.on_message(u2, MagicMock()))
        self.assertEqual(bot.connect_sessions[chat_id]["host"], "103.76.214.2")
        self.assertEqual(bot.connect_sessions[chat_id]["port"], 65430)

        u3, _ = make_update("Administrator", chat_id=chat_id)
        asyncio.run(bot.on_message(u3, MagicMock()))
        u4, _ = make_update("secret123", chat_id=chat_id)
        asyncio.run(bot.on_message(u4, MagicMock()))

        fake.connect.assert_awaited_once()
        self.assertNotIn(chat_id, bot.connect_sessions)
        self.assertIn("rdp1", bot.profiles)
        self.assertEqual(bot.current["host"], "103.76.214.2")
        self.assertEqual(bot.current["port"], 65430)
        self.assertEqual(bot.current["user"], "Administrator")
        self.assertEqual(bot.current["password"], "secret123")

    def test_cancel(self):
        self.use_fake()
        update, _ = make_update("/connect")
        ctx = MagicMock()
        ctx.args = []
        asyncio.run(bot.cmd_connect(update, ctx))
        self.assertIn(555, bot.connect_sessions)
        u2, _ = make_update("/cancel")
        asyncio.run(bot.cmd_cancel(u2, MagicMock()))
        self.assertNotIn(555, bot.connect_sessions)


class TestWinCtrl(BotTestCase):
    @staticmethod
    def img(color):
        from PIL import Image

        return Image.new("RGB", (120, 80), color)

    def test_win_no_word(self):
        self.use_fake()
        update, msg = make_update("/win")
        asyncio.run(bot.cmd_win(update, MagicMock()))
        self.assertIn("Not found", msg.reply_text.await_args[0][0])

    def test_win_opened(self):
        self.use_fake()
        fake = bot.get_mgr()
        update, msg = make_update("/win chrome")
        ctx = MagicMock()
        ctx.args = ["chrome"]
        fake.screenshot = AsyncMock(side_effect=[self.img((0, 0, 0)), self.img((255, 255, 255))])
        asyncio.run(bot.cmd_win(update, ctx))
        self.assertIn("Opened", msg.reply_text.await_args[0][0])

    def test_win_not_found(self):
        self.use_fake()
        fake = bot.get_mgr()
        update, msg = make_update("/win zzznotanapp")
        ctx = MagicMock()
        ctx.args = ["zzznotanapp"]
        same = self.img((10, 10, 10))
        fake.screenshot = AsyncMock(side_effect=[same, same])
        asyncio.run(bot.cmd_win(update, ctx))
        self.assertIn("Not found", msg.reply_text.await_args[0][0])

    def test_ctrl(self):
        fake = self.use_fake()
        update, msg = make_update("/ctrl hello")
        ctx = MagicMock()
        ctx.args = ["hello"]
        asyncio.run(bot.cmd_ctrl(update, ctx))
        self.assertIn("Typed", msg.reply_text.await_args[0][0])
        actions = fake.execute.await_args[0][0]
        self.assertEqual(actions[0], ("key", "ctrl+a"))
        self.assertEqual(actions[2], ("text", "hello"))

    def test_diff_ratio(self):
        self.assertEqual(bot.diff_ratio(self.img((0, 0, 0)), self.img((0, 0, 0))), 0.0)
        self.assertGreater(bot.diff_ratio(self.img((0, 0, 0)), self.img((255, 255, 255))), 0.9)


if __name__ == "__main__":
    unittest.main()
