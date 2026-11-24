"""Conservative validation for Automa JSON received through the bot.

This is a pre-import safety gate, not an Automa execution engine. Workflows with
loops, schedules, custom code, nested workflows, or ambiguous repeated clicks are
rejected. It cannot prove that arbitrary browser automation is harmless, so an
accepted workflow still requires explicit per-chat confirmation before any send.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_WORKFLOW_BYTES = 1_000_000
MAX_NODES = 150
_BLOCKED = (
    "loop", "repeat", "javascript", "script", "http-request",
    "execute-workflow", "webhook", "schedule", "interval",
)


class WorkflowValidationError(ValueError):
    pass


@dataclass(frozen=True)
class WorkflowSummary:
    name: str
    node_count: int
    send_clicks: int


def validate_automa_json(raw: bytes) -> tuple[dict[str, Any], WorkflowSummary]:
    """Parse and conservatively validate an exported Automa workflow JSON."""
    if not raw or len(raw) > MAX_WORKFLOW_BYTES:
        raise WorkflowValidationError("JSON file must be between 1 byte and 1 MB.")
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowValidationError("File is not valid UTF-8 JSON.") from exc
    if not isinstance(doc, dict):
        raise WorkflowValidationError("Workflow root must be a JSON object.")
    name = doc.get("name")
    nodes = (doc.get("drawflow") or {}).get("nodes") if isinstance(doc.get("drawflow"), dict) else None
    if not isinstance(name, str) or not name.strip() or len(name) > 120:
        raise WorkflowValidationError("Workflow needs a name (up to 120 characters).")
    if not isinstance(nodes, list) or not nodes or len(nodes) > MAX_NODES:
        raise WorkflowValidationError(f"Workflow must contain 1–{MAX_NODES} blocks.")

    send_clicks = 0
    for node in nodes:
        if not isinstance(node, dict):
            raise WorkflowValidationError("Every workflow block must be an object.")
        label = str(node.get("label", "")).lower()
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        block_kind = str(data.get("type", "")).lower()
        type_name = str(node.get("type", "")).lower()
        descriptor = " ".join((label, block_kind, type_name))
        if any(word in descriptor for word in _BLOCKED):
            raise WorkflowValidationError(
                f"Block '{node.get('label', 'unknown')}' is not allowed (loop/code/network/schedule/nested workflow)."
            )
        if label == "trigger":
            trigger_type = str(data.get("type", "manual")).lower()
            if trigger_type not in ("manual", ""):
                raise WorkflowValidationError("Only manual workflow triggers are allowed.")
        if label in ("event-click", "click-element"):
            if bool(data.get("multiple", False)):
                raise WorkflowValidationError("Repeated/multiple-click blocks are not allowed.")
            selector = str(data.get("selector", "")).lower()
            if "send" in selector or "submit" in selector:
                send_clicks += 1
                on_error = data.get("onError") if isinstance(data.get("onError"), dict) else {}
                if on_error.get("retry") is True or int(on_error.get("retryTimes", 0) or 0) > 1:
                    raise WorkflowValidationError("A Send click cannot retry.")
        if bool(data.get("multiple", False)) and label in ("event-click", "click-element"):
            raise WorkflowValidationError("Repeated/multiple-click blocks are not allowed.")
    if send_clicks > 1:
        raise WorkflowValidationError("At most one possible Send/Submit click is allowed per workflow.")
    return doc, WorkflowSummary(name=name.strip(), node_count=len(nodes), send_clicks=send_clicks)
