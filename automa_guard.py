"""Minimal JSON parsing for Automa workflow uploads.

This module checks only that the upload is a reasonably sized JSON object. It
intentionally does not block workflow node types, loops, retries, multiple
clicks, schedules, scripts, or network-related blocks. Automa remains responsible
for interpreting its own workflow format.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_WORKFLOW_BYTES = 20 * 1024 * 1024


class WorkflowValidationError(ValueError):
    pass


@dataclass(frozen=True)
class WorkflowSummary:
    name: str
    node_count: int
    send_clicks: int


def validate_automa_json(raw: bytes) -> tuple[dict[str, Any], WorkflowSummary]:
    """Parse any Automa workflow JSON without filtering its workflow contents."""
    if not raw or len(raw) > MAX_WORKFLOW_BYTES:
        raise WorkflowValidationError("JSON file must be between 1 byte and 20 MB.")
    try:
        doc = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkflowValidationError("File is not valid UTF-8 JSON.") from exc
    if not isinstance(doc, dict):
        raise WorkflowValidationError("Automa workflow JSON root must be an object.")

    name = doc.get("name")
    if not isinstance(name, str) or not name.strip():
        name = "Imported workflow"
    drawflow = doc.get("drawflow")
    nodes = drawflow.get("nodes") if isinstance(drawflow, dict) else None
    if not isinstance(nodes, list):
        nodes = []
    send_clicks = 0
    for node in nodes:
        if not isinstance(node, dict):
            continue
        data = node.get("data") if isinstance(node.get("data"), dict) else {}
        descriptor = " ".join((str(node.get("label", "")), str(data.get("selector", "")))).lower()
        if "send" in descriptor or "submit" in descriptor:
            send_clicks += 1
    return doc, WorkflowSummary(name=name.strip()[:200], node_count=len(nodes), send_clicks=send_clicks)
