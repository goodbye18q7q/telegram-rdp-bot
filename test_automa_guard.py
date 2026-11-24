import json
import unittest

from automa_guard import WorkflowValidationError, MAX_WORKFLOW_BYTES, validate_automa_json


def sample_workflow():
    return {
        "name": "One Greeting",
        "drawflow": {
            "nodes": [
                {"id": "trigger", "label": "trigger", "data": {"type": "interval"}},
                {"id": "loop", "label": "loop-data", "data": {"type": "loop-data"}},
                {"id": "script", "label": "javascript", "data": {"code": "return true"}},
                {"id": "send1", "label": "event-click", "data": {
                    "selector": "button[aria-label=\"Send\"]", "multiple": True,
                    "onError": {"retry": True, "retryTimes": 10},
                }},
                {"id": "send2", "label": "event-click", "data": {"selector": "button.submit"}},
            ]
        },
        "settings": {"execContext": "popup", "custom": True},
    }


class TestAutomaGuard(unittest.TestCase):
    def test_accepts_repeat_script_schedule_multiple_sends_and_retries(self):
        source = sample_workflow()
        raw = json.dumps(source).encode()
        doc, summary = validate_automa_json(raw)
        self.assertEqual(summary.name, "One Greeting")
        self.assertEqual(summary.node_count, 5)
        self.assertEqual(summary.send_clicks, 2)
        self.assertEqual(doc, source)

    def test_accepts_workflow_without_active_tab_or_node_list(self):
        source = {"name": "Other workflow format", "settings": {"execContext": "popup"}}
        doc, summary = validate_automa_json(json.dumps(source).encode())
        self.assertEqual(doc, source)
        self.assertEqual(summary.node_count, 0)

    def test_rejects_invalid_json_non_object_and_oversize_only(self):
        with self.assertRaisesRegex(WorkflowValidationError, "valid UTF-8 JSON"):
            validate_automa_json(b"not json")
        with self.assertRaisesRegex(WorkflowValidationError, "root must be an object"):
            validate_automa_json(json.dumps([1, 2, 3]).encode())
        with self.assertRaisesRegex(WorkflowValidationError, "20 MB"):
            validate_automa_json(b" " * (MAX_WORKFLOW_BYTES + 1))


if __name__ == "__main__":
    unittest.main()
