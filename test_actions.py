"""Parser + action engine ke unit tests (bina real RDP server ke).

Chalane ke liye:  python -m unittest test_actions -v
"""
from __future__ import annotations

import unittest

import actions
import commands


class FakeClient:
    """RDPClient ka fake — calls record karta hai."""

    def __init__(self):
        self.width = 1366
        self.height = 768
        self.calls = []

    async def send_key(self, key, mode="press"):
        self.calls.append(("key", key, mode))

    async def send_text(self, text, chars_per_second=10.0):
        self.calls.append(("text", text))

    async def mouse_move(self, x, y):
        self.calls.append(("move", x, y))

    async def mouse_click(self, x, y, button=1, double_click=False):
        self.calls.append(("click", x, y, button, double_click))

    async def mouse_wheel(self, x, y, delta):
        self.calls.append(("wheel", x, y, delta))


class TestCombo(unittest.TestCase):
    def test_simple(self):
        self.assertEqual(actions.parse_combo("enter"), ("enter", []))

    def test_ctrl_shift_esc(self):
        key, mods = actions.parse_combo("ctrl+shift+esc")
        self.assertEqual(key, "esc")
        self.assertEqual(mods, ["ctrl", "shift"])

    def test_win_r(self):
        self.assertEqual(actions.parse_combo("win+r"), ("r", ["win"]))

    def test_only_modifier(self):
        self.assertEqual(actions.parse_combo("ctrl"), ("ctrl", []))


class TestParser(unittest.TestCase):
    def test_open_chrome(self):
        acts, _ = commands.parse_command("open chrome")
        self.assertIn(("key", "ctrl+esc"), acts)
        self.assertIn(("text", "chrome"), acts)

    def test_open_instagram_web(self):
        acts, _ = commands.parse_command("open instagram")
        self.assertTrue(any(a == ("text", "https://www.instagram.com") for a in acts))

    def test_kholo_hinglish(self):
        acts, _ = commands.parse_command("chrome kholo")
        self.assertIn(("text", "chrome"), acts)

    def test_search(self):
        acts, _ = commands.parse_command("/search instagram")
        self.assertIn(("text", "instagram"), acts)
        self.assertIn(("key", "ctrl+l"), acts)

    def test_install(self):
        acts, _ = commands.parse_command("install adblock plus")
        self.assertTrue(any(a[0] == "text" and "chromewebstore" in a[1] for a in acts))

    def test_cmd(self):
        acts, _ = commands.parse_command("/cmd ipconfig")
        self.assertIn(("key", "ctrl+esc"), acts)
        self.assertIn(("text", "powershell"), acts)
        self.assertIn(("text", "ipconfig"), acts)

    def test_key_combo(self):
        acts, _ = commands.parse_command("/key ctrl+alt+delete")
        self.assertEqual(acts, [("key", "ctrl+alt+delete")])

    def test_click(self):
        acts, _ = commands.parse_command("/click 100 200 right")
        self.assertEqual(acts, [("click", 100, 200, 2, False)])

    def test_seq(self):
        acts = commands.parse_sequence("win+r; type chrome; press enter; wait 2")
        self.assertEqual(acts[0], ("key", "win+r"))
        self.assertEqual(acts[1], ("text", "chrome"))
        self.assertEqual(acts[2], ("key", "enter"))
        self.assertEqual(acts[3], ("wait", 2.0))

    def test_unknown(self):
        with self.assertRaises(ValueError):
            commands.parse_command("asdfghjkl")

    def test_a_win_search(self):
        acts = commands.a_win_search("chrome")
        self.assertIn(("key", "ctrl+esc"), acts)
        self.assertIn(("text", "chrome"), acts)
        self.assertNotIn(("key", "enter"), acts)  # Enter is pressed later (for detection)

    def test_open_clears_existing_text(self):
        # Win+R box me purana text ho to pehle clear ho (ctrl+a + backspace)
        for acts in (commands.a_win_search("chrome"), commands.a_open_app("chrome")):
            i = acts.index(("key", "ctrl+a"))
            self.assertEqual(acts[i + 1], ("wait", 0.2))
            self.assertEqual(acts[i + 2], ("key", "backspace"))

    def test_edge(self):
        self.assertEqual(commands.parse_command("edge")[1], "Opening Microsoft Edge")
        self.assertIn(("text", "msedge"), commands.parse_command("open edge")[0])
        self.assertIn("msedge", commands.APP_ALIASES["msedge"])


class TestApply(unittest.IsolatedAsyncioTestCase):
    async def test_apply_combo(self):
        c = FakeClient()
        await actions.apply_action(c, ("key", "ctrl+shift+esc"), {"type_cps": 12})
        self.assertEqual(
            c.calls,
            [
                ("key", "ctrl", "hold"),
                ("key", "shift", "hold"),
                ("key", "esc", "press"),
                ("key", "shift", "release"),
                ("key", "ctrl", "release"),
            ],
        )

    async def test_apply_text_click_scroll(self):
        c = FakeClient()
        await actions.apply_action(c, ("text", "hello"), {"type_cps": 12})
        await actions.apply_action(c, ("click", 5, 6, 1, False), {"type_cps": 12})
        await actions.apply_action(c, ("scroll", -360), {"type_cps": 12})
        self.assertEqual(c.calls[0], ("text", "hello"))
        self.assertEqual(c.calls[1], ("click", 5, 6, 1, False))
        self.assertEqual(c.calls[2], ("wheel", 683, 384, -360))

    async def test_apply_full_plan(self):
        """a_search ka poora plan fake client par chale."""
        c = FakeClient()
        cfg = {"type_cps": 12}
        for act in commands.a_search("instagram"):
            await actions.apply_action(c, act, cfg)
        kinds = [call[0] for call in c.calls]
        self.assertEqual(kinds[0], "key")  # ctrl+esc
        self.assertIn(("text", "instagram"), c.calls)


if __name__ == "__main__":
    unittest.main()
