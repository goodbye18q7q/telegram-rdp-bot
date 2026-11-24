"""Short-lived state and transfer primitives for the Automa Telegram flow."""
from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field

MAX_SELECTED_GROUPS = 5
MAX_WORKFLOW_BYTES = 1_000_000


@dataclass(frozen=True)
class GroupChoice:
    index: int
    label: str
    row_y: int


@dataclass
class ChatFlow:
    choices: list[GroupChoice] = field(default_factory=list)
    selected: set[int] = field(default_factory=set)
    confirmed: bool = False
    workflow_name: str | None = None
    workflow_bytes: bytes | None = None
    workflow_digest: str | None = None
    completed_targets: set[str] = field(default_factory=set)
    send_clicks: int = 0
    profile: str | None = None
    active_tabs: list[int] = field(default_factory=list)
    run_state: str | None = None
    run_completed: int = 0

    def set_choices(self, choices: list[GroupChoice]) -> None:
        self.choices = choices[:MAX_SELECTED_GROUPS]
        self.selected.clear()
        self.confirmed = False
        self.active_tabs = []

    def toggle(self, index: int) -> bool:
        if index not in {choice.index for choice in self.choices}:
            raise ValueError("Unknown chat choice")
        if index in self.selected:
            self.selected.remove(index)
            self.confirmed = False
            return False
        if len(self.selected) >= MAX_SELECTED_GROUPS:
            raise ValueError("Choose no more than five groups")
        self.selected.add(index)
        self.confirmed = False
        return True

    def finish_selection(self) -> list[GroupChoice]:
        if not self.selected:
            raise ValueError("Select at least one group before /done")
        self.confirmed = True
        return [choice for choice in self.choices if choice.index in self.selected]


@dataclass(frozen=True)
class VaultEntry:
    filename: str
    content: bytes
    expires_at: float


class WorkflowVault:
    """In-memory, unguessable, expiring download tokens; no files on bot disk."""
    def __init__(self, ttl_seconds: int = 600):
        self.ttl_seconds = ttl_seconds
        self.entries: dict[str, VaultEntry] = {}

    def put(self, filename: str, content: bytes) -> str:
        self.purge()
        token = secrets.token_urlsafe(32)
        clean = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not clean.lower().endswith(".json"):
            clean += ".json"
        self.entries[token] = VaultEntry(clean[:100], bytes(content), time.time() + self.ttl_seconds)
        return token

    def get(self, token: str) -> VaultEntry | None:
        self.purge()
        return self.entries.get(token)

    def discard(self, token: str) -> None:
        self.entries.pop(token, None)

    def purge(self) -> None:
        now = time.time()
        for key in [key for key, entry in self.entries.items() if entry.expires_at <= now]:
            self.entries.pop(key, None)
