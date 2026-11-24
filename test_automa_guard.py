import json
import unittest

from automa_guard import WorkflowValidationError, validate_automa_json


def sample_workflow():
    return {
        "name": "One Greeting",
        "drawflow": {
            "nodes": [
                {"id": "trigger", "label": "trigger", "data": {"type": "manual"}},
                {"id": "active", "label": "active-tab", "data": {}},
                {"id": "fill", "label": "forms", "data": {"type": "text-field", "value": "Hello"}},
                {"id": "send", "label": "event-click", "data": {
                    "selector": "button[aria-label=\"Send\"]", "multiple": False,
                    "onError": {"retry": False, "retryTimes": 1},
                }},
            ]
        },
        "settings": {"execContext": "popup"},
    }


class TestAutomaGuard(unittest.TestCase):
    def test_accepts_one_manual_send_sample(self):
        doc, summary = validate_automa_json(json.dumps(sample_workflow()).encode())
        self.assertEqual(summary.name, "One Greeting")
        self.assertEqual(summary.send_clicks, 1)
        self.assertEqual(summary.node_count, 4)
        self.assertEqual(doc["settings"]["execContext"], "popup")

    def test_rejects_loop_blocks(self):
        doc = sample_workflow()
        doc["drawflow"]["nodes"].append({"label": "loop-data", "data": {"type": "loop-data"}})
        with self.assertRaisesRegex(WorkflowValidationError, "not allowed"):
            validate_automa_json(json.dumps(doc).encode())

    def test_rejects_nonmanual_trigger(self):
        doc = sample_workflow()
        doc["drawflow"]["nodes"][0]["data"]["type"] = "interval"
        with self.assertRaises(WorkflowValidationError):
            validate_automa_json(json.dumps(doc).encode())

    def test_rejects_multiple_send_clicks(self):
        doc = sample_workflow()
        clone = json.loads(json.dumps(doc["drawflow"]["nodes"][-1]))
        clone["id"] = "click-send-twice"
        doc["drawflow"]["nodes"].append(clone)
        with self.assertRaisesRegex(WorkflowValidationError, "At most one"):
            validate_automa_json(json.dumps(doc).encode())

    def test_rejects_retried_send_click(self):
        doc = sample_workflow()
        doc["drawflow"]["nodes"][-1]["data"]["onError"]["retry"] = True
        with self.assertRaisesRegex(WorkflowValidationError, "cannot retry"):
            validate_automa_json(json.dumps(doc).encode())

    def test_rejects_non_json_and_oversize(self):
        with self.assertRaises(WorkflowValidationError):
            validate_automa_json(b"not json")
        with self.assertRaisesRegex(WorkflowValidationError, "1 MB"):
            validate_automa_json(b" " * 1_000_001)


if __name__ == "__main__":
    unittest.main()
