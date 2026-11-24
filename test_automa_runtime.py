from __future__ import annotations

import unittest
from unittest.mock import patch

from automa_runtime import ChatFlow, GroupChoice, WorkflowVault


class TestChatFlow(unittest.TestCase):
    def setUp(self):
        self.flow = ChatFlow()
        self.flow.set_choices([GroupChoice(i, f"Group {i}", 400 + 70*i) for i in range(1, 7)])

    def test_at_most_five_choices_and_selection(self):
        self.assertEqual(len(self.flow.choices), 5)
        self.flow.toggle(1)
        self.flow.toggle(3)
        selected = self.flow.finish_selection()
        self.assertEqual([x.index for x in selected], [1, 3])
        self.assertTrue(self.flow.confirmed)

    def test_empty_selection_requires_done_again(self):
        with self.assertRaisesRegex(ValueError, "at least one"):
            self.flow.finish_selection()

    def test_unknown_choice_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unknown"):
            self.flow.toggle(6)

    def test_deselect_invalidates_done(self):
        self.flow.toggle(2)
        self.flow.finish_selection()
        self.flow.toggle(2)
        self.assertFalse(self.flow.confirmed)


class TestWorkflowVault(unittest.TestCase):
    def test_short_lived_secure_file_entry(self):
        vault = WorkflowVault()
        token = vault.put("../unsafe/flow.json", b"{}")
        entry = vault.get(token)
        self.assertEqual(entry.filename, "flow.json")
        self.assertEqual(entry.content, b"{}")
        self.assertGreaterEqual(len(token), 40)
        vault.discard(token)
        self.assertIsNone(vault.get(token))

    def test_expiry(self):
        vault = WorkflowVault(ttl_seconds=2)
        with patch("automa_runtime.time.time", return_value=100):
            token = vault.put("flow.json", b"{}")
        with patch("automa_runtime.time.time", return_value=103):
            self.assertIsNone(vault.get(token))


if __name__ == "__main__":
    unittest.main()
