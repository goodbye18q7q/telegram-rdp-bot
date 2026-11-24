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
        bot.ig_login_sessions.clear()
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
            "cmd_igdm", "on_igdm_callback", "on_message",
        ):
            self.assertTrue(hasattr(bot, name), f"missing handler: {name}")

    def test_open_chrome(self):
        fake = self.use_fake()
        update, msg = make_update("open chrome")
        asyncio.run(bot.on_message(update, MagicMock()))
        fake.execute.assert_awaited_once()
        actions = fake.execute.await_args[0][0]
        self.assertIn(("key", "ctrl+esc"), actions)
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


class TestInstagramOtp(BotTestCase):
    def test_otp_targets_email_code_and_continue_controls(self):
        from types import SimpleNamespace
        fake = self.use_fake()
        fake.client = SimpleNamespace(width=1366, height=768)
        fake.ensure = AsyncMock()
        update, msg = make_update("/igotp 123456")
        update.effective_chat.type = "private"
        update.effective_chat.send_message = AsyncMock()
        update.effective_user.id = 111
        msg.delete = AsyncMock()
        bot.cfg["allowed_users"] = [111]

        asyncio.run(bot.cmd_igotp(update, MagicMock(args=["123456"])))

        self.assertTrue(msg.delete.awaited)
        actions = [action for call in fake.execute.await_args_list for action in call.args[0]]
        self.assertIn(("click", 683, 614, 1, False), actions)
        self.assertIn(("click", 683, 733, 1, False), actions)
        self.assertEqual([a[1] for a in actions if a[0] == "text"], list("123456"))
        self.assertNotIn("123456", bot.ig_login_sessions[555].values())


class TestInstagramDmButton(BotTestCase):
    def test_igdm_command_sends_private_inline_button(self):
        update, msg = make_update("/igdm")
        update.effective_chat.type = "private"
        bot.cfg["allowed_users"] = [111]

        asyncio.run(bot.cmd_igdm(update, MagicMock()))

        markup = msg.reply_text.await_args.kwargs["reply_markup"]
        self.assertEqual(markup.inline_keyboard[0][0].callback_data, "igdm:open")
        self.assertIn("koi message send nahi karta", msg.reply_text.await_args.args[0])

    def test_igdm_button_clicks_left_rail_messages_icon(self):
        from types import SimpleNamespace
        fake = self.use_fake()
        fake.client = SimpleNamespace(width=1366, height=768)
        fake.ensure = AsyncMock()
        bot.cfg["allowed_users"] = [111]
        query = MagicMock()
        query.answer = AsyncMock()
        query.message.reply_text = AsyncMock()
        update = MagicMock()
        update.callback_query = query
        update.effective_user.id = 111
        update.effective_chat.type = "private"

        asyncio.run(bot.on_igdm_callback(update, MagicMock()))

        actions = fake.execute.await_args[0][0]
        self.assertIn(("click", 35, 339, 1, False), actions)
        self.assertIn("koi DM send nahi hua", query.message.reply_text.await_args.args[0])


class TestWebNavigation(BotTestCase):
    def test_full_windows_rdp_uses_browser_address_bar_not_start_search(self):
        fake = self.use_fake()
        result = asyncio.run(bot._open_url_for_profile(fake, "https://www.instagram.com", "windows-rdp"))
        actions = fake.execute.await_args[0][0]
        self.assertIn(("key", "ctrl+l"), actions)
        self.assertIn(("key", "escape"), actions)
        self.assertNotIn(("key", "ctrl+esc"), actions)
        self.assertEqual([a[1] for a in actions if a[0] == "text"], ["https://www.instagram.com"])
        self.assertIn("current browser", result)


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

    def test_switch1_alias(self):
        self.save_profile(name="rdp1", host="10.0.0.1")
        fake = self.use_fake()
        update, msg = make_update("switch1")
        asyncio.run(bot.on_message(update, MagicMock()))
        fake.connect.assert_awaited_once()
        self.assertEqual(bot.current["name"], "rdp1")

    def test_switch_slash_command(self):
        self.save_profile(name="rdp1", host="10.0.0.1")
        fake = self.use_fake()
        update, msg = make_update("/switch 1")
        ctx = MagicMock()
        ctx.args = ["1"]
        asyncio.run(bot.cmd_switch(update, ctx))
        fake.connect.assert_awaited_once()
        self.assertEqual(bot.current["name"], "rdp1")

    def test_connect_is_always_fresh(self):
        # Pehle se connected ho to bhi /connect purani band karke naya khole
        self.save_profile(name="rdp1", host="10.0.0.1")
        old = fake_manager()
        bot.managers["rdp1"] = old
        bot.get_mgr = lambda name=None: old
        update, msg = make_update("/connect rdp1")
        ctx = MagicMock()
        ctx.args = ["rdp1"]
        asyncio.run(bot.cmd_connect(update, ctx))
        old.disconnect.assert_awaited_once()
        old.connect.assert_awaited_once()

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

    def test_one_shot_with_port(self):
        fake = self.use_fake()
        update, msg = make_update("/connect 8.8.8.8 65430 admin pw")
        ctx = MagicMock()
        ctx.args = ["8.8.8.8", "65430", "admin", "pw"]
        asyncio.run(bot.cmd_connect(update, ctx))
        fake.connect.assert_awaited_once()
        self.assertEqual(bot.current["port"], 65430)
        self.assertEqual(bot.current["user"], "admin")

    def test_garbage_host_rejected(self):
        # User ne bot ka prompt hi copy karke IP bhej diya tha -> reject hona chahiye
        self.use_fake()
        update, msg = make_update("/connect")
        ctx = MagicMock()
        ctx.args = []
        asyncio.run(bot.cmd_connect(update, ctx))
        self.assertEqual(bot.connect_sessions[555]["step"], "ip")

        prompt = "🌐 Host: 202.140.136.146  Port: 8250\n👤 Ab username bhejo:"
        u2, m2 = make_update(prompt, chat_id=555)
        asyncio.run(bot.on_message(u2, MagicMock()))
        self.assertIn("Ye IP nahi lag raha", m2.reply_text.await_args[0][0])
        # step wahi rakho — galat input par loop na ho
        self.assertEqual(bot.connect_sessions[555]["step"], "ip")

        u3, m3 = make_update("202.140.136.146:8250", chat_id=555)
        asyncio.run(bot.on_message(u3, MagicMock()))
        self.assertEqual(bot.connect_sessions[555]["host"], "202.140.136.146")
        self.assertEqual(bot.connect_sessions[555]["port"], 8250)

        u4, m4 = make_update("admin", chat_id=555)
        asyncio.run(bot.on_message(u4, MagicMock()))
        u5, m5 = make_update("secret", chat_id=555)
        asyncio.run(bot.on_message(u5, MagicMock()))
        fake = bot.get_mgr()
        fake.connect.assert_awaited_once()
        self.assertEqual(bot.current["host"], "202.140.136.146")
        self.assertEqual(bot.current["port"], 8250)
        self.assertEqual(bot.current["user"], "admin")

    def test_validate_host(self):
        for good in ("1.2.3.4", "1.2.3.4:65430", "rdp.example.com", "[2001:db8::1]:3389", "my-host_1"):
            self.assertIsNotNone(bot.validate_host(good), good)
        for bad in (
            "",
            "🌐 Host: 1.2.3.4  Port: 8250",
            "1.2.3.4\n👤 Now send the username:",
            "hello world",
            "!!!",
        ):
            self.assertIsNone(bot.validate_host(bad), repr(bad))

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
        # desktop(black) -> dialog(gray) -> app(white)
        self.use_fake()
        fake = bot.get_mgr()
        update, msg = make_update("/win chrome")
        ctx = MagicMock()
        ctx.args = ["chrome"]
        fake.screenshot = AsyncMock(
            side_effect=[self.img((0, 0, 0)), self.img((80, 80, 80)),
                         self.img((255, 255, 255)), self.img((255, 255, 255))]
        )
        asyncio.run(bot.cmd_win(update, ctx))
        self.assertIn("Opened", msg.reply_text.await_args[0][0])

    def test_win_not_found(self):
        # dialog khulta hai par app nahi — screen wapas desktop jaisi
        self.use_fake()
        fake = bot.get_mgr()
        update, msg = make_update("/win zzznotanapp")
        ctx = MagicMock()
        ctx.args = ["zzznotanapp"]
        desktop = self.img((0, 0, 0))
        fake.screenshot = AsyncMock(
            side_effect=[desktop, self.img((80, 80, 80)), desktop, desktop]
        )
        asyncio.run(bot.cmd_win(update, ctx))
        self.assertIn("Not found", msg.reply_text.await_args[0][0])

    def test_win_dialog_did_not_open(self):
        self.use_fake()
        fake = bot.get_mgr()
        update, msg = make_update("/win chrome")
        ctx = MagicMock()
        ctx.args = ["chrome"]
        same = self.img((10, 10, 10))
        fake.screenshot = AsyncMock(side_effect=[same] * 16)
        asyncio.run(bot.cmd_win(update, ctx))
        text = msg.reply_text.await_args[0][0]
        self.assertIn("Start menu", text)
        self.assertNotIn("Opened", text)

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


class TestRdpPatch(unittest.IsolatedAsyncioTestCase):
    """simple-rdp ka 0x17 (Data PDU) tolerance patch."""

    def test_patch_applied(self):
        import simple_rdp.client as cm

        self.assertTrue(
            getattr(cm.RDPClient._handle_licensing, "_rdpbot_tolerant_patched", False)
        )
        self.assertTrue(
            getattr(cm.RDPClient._handle_capability_exchange, "__name__", "") == "_tolerant_capability_exchange"
        )

    async def test_skips_data_pdu_then_succeeds(self):
        import simple_rdp.client as cm

        calls = {"n": 0}

        async def fake_original(self):
            calls["n"] += 1
            if calls["n"] < 3:
                raise ConnectionError("Expected Demand Active PDU, got type 0x17")
            return "ok"

        import rdp_patch

        real = rdp_patch._orig_capability
        rdp_patch._orig_capability = fake_original
        try:
            result = await rdp_patch._tolerant_capability_exchange(object())
            self.assertEqual(result, "ok")
            self.assertEqual(calls["n"], 3)
        finally:
            rdp_patch._orig_capability = real

    async def test_gives_up_after_max_skip(self):
        import rdp_patch

        async def always_bad(self):
            raise ConnectionError("Expected Demand Active PDU, got type 0x17")

        real = rdp_patch._orig_capability
        rdp_patch._orig_capability = always_bad
        try:
            with self.assertRaises(ConnectionError):
                await rdp_patch._tolerant_capability_exchange(object())
        finally:
            rdp_patch._orig_capability = real

    async def test_other_errors_propagate(self):
        import rdp_patch

        async def boom(self):
            raise ConnectionError("auth failed")

        real = rdp_patch._orig_capability
        rdp_patch._orig_capability = boom
        try:
            with self.assertRaises(ConnectionError):
                await rdp_patch._tolerant_capability_exchange(object())
        finally:
            rdp_patch._orig_capability = real

    async def test_real_method_skips_0x17(self):
        """Asli _handle_capability_exchange: pehle 0x17, phir Demand Active -> success."""
        import simple_rdp.client as cm

        # 0x17 = Data PDU (6-byte share control header), 0x11 = Demand Active
        data_pdu = b"\x00\x00" + b"\x17\x00" + b"\x00\x00" + b"\x00" * 20
        active_pdu = b"\x00\x00" + b"\x11\x00" + b"\x00\x00" + b"\x00" * 40

        sent = []

        class Stub:
            _width, _height, _color_depth, _io_channel_id = 1920, 1080, 32, 1003

            def __init__(self):
                self._pending_data = None
                self._share_id = None

            async def _recv_x224_data(self):
                return active_pdu  # pehli baar Data PDU pending se aayega

            def _extract_pdu_from_mcs(self, response):
                return response

            async def _send_mcs_data(self, data, channel):
                sent.append(data)

            def _build_share_control_pdu(self, pdu_type, data):
                return b"\x00" * 6 + data

        stub = Stub()
        stub._pending_data = data_pdu

        real_parse = cm.parse_demand_active_pdu
        cm.parse_demand_active_pdu = lambda b: {"share_id": 1234}
        try:
            await cm.RDPClient._handle_capability_exchange(stub)
        finally:
            cm.parse_demand_active_pdu = real_parse

        self.assertEqual(stub._share_id, 1234)
        self.assertTrue(sent, "Confirm Active PDU bheja jana chahiye")


if __name__ == "__main__":
    unittest.main()
