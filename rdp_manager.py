"""RDP connection manager — simple-rdp ke RDPClient ka async wrapper."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from simple_rdp import RDPClient

import rdp_patch
from actions import apply_action

rdp_patch.apply()

log = logging.getLogger(__name__)

CONNECT_ATTEMPTS = 3


class RDPManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client: RDPClient | None = None
        self._exec_lock = asyncio.Lock()

    @property
    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    async def connect(self) -> None:
        if self.is_connected:
            return
        # Purana dead client cleanup
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.disconnect()
            self.client = None

        cfg = self.cfg
        if not cfg["rdp_host"]:
            raise ConnectionError("RDP_HOST set nahi hai — env ya /connect args me do")

        client = RDPClient(
            host=cfg["rdp_host"],
            port=cfg["rdp_port"],
            username=cfg["rdp_user"] or None,
            password=cfg["rdp_pass"] or None,
            domain=cfg["rdp_domain"] or None,
            width=cfg["rdp_width"],
            height=cfg["rdp_height"],
            capture_fps=cfg["capture_fps"],
        )

        # RDP handshake kabhi-kabhi flaky hota hai (0x17 jaise PDU issues) — retry.
        last: Exception | None = None
        for attempt in range(1, CONNECT_ATTEMPTS + 1):
            try:
                await client.connect()
                break
            except Exception as e:
                last = e
                log.warning("connect attempt %d/%d failed: %s", attempt, CONNECT_ATTEMPTS, e)
                with contextlib.suppress(Exception):
                    await client.disconnect()
                if attempt < CONNECT_ATTEMPTS:
                    await asyncio.sleep(1.0 * attempt)
        else:
            raise last  # type: ignore[misc]

        self.client = client
        log.info("RDP connected: %s:%s", cfg["rdp_host"], cfg["rdp_port"])

    async def disconnect(self) -> None:
        client, self.client = self.client, None
        if client is not None:
            with contextlib.suppress(Exception):
                await client.disconnect()
            log.info("RDP disconnected")

    async def ensure(self) -> None:
        if not self.is_connected:
            await self.connect()

    async def execute(self, actions: list) -> None:
        """Action sequence ko sequentially chalata hai (ek waqt me ek hi)."""
        await self.ensure()
        async with self._exec_lock:
            for action in actions:
                await apply_action(self.client, action, self.cfg)

    async def screenshot(self):
        """Remote desktop ki current photo (PIL Image)."""
        await self.ensure()
        return await self.client.screenshot()

    def status_text(self) -> str:
        if self.is_connected:
            return (
                f"🟢 Connected — {self.cfg['rdp_host']}:{self.cfg['rdp_port']} "
                f"({self.client.width}x{self.client.height})"
            )
        return "🔴 Disconnected"
