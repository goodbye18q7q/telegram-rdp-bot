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


def _brightness(img) -> float:
    """PIL image ki average brightness (0-255)."""
    h = img.convert("L").histogram()
    total = sum(h)
    return sum(i * c for i, c in enumerate(h)) / total if total else 0.0

CONNECT_ATTEMPTS = 3
# Server Demand Active ke baad pehla screen kuch seconds late bhejta hai.
# Jab tak bitmap updates na aayein, screenshot black dikhta hai.
SCREEN_WAIT_TIMEOUT = 30.0


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
            color_depth=cfg.get("rdp_color_depth", 32),
            # Zaroori: False (default) PEH_PERF_DISABLE_WALLPAPER bhejta hai,
            # jisse server poora desktop black/solid render karta hai.
            show_wallpaper=True,
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
                msg = str(e)
                # Give helpful hint for common NLA/CredSSP failures
                if "3221225581" in msg or "CredSSP" in msg:
                    msg += " — CredSSP/NLA verification failed. Username/password sahi hai? Domain chahiye? (e.g. TechTally\\Ajay)"
                elif "LOGON_FAILURE" in msg:
                    msg += " — Logon failed. Username/password ya domain check karo."
                log.warning("connect attempt %d/%d failed: %s", attempt, CONNECT_ATTEMPTS, msg)
                with contextlib.suppress(Exception):
                    await client.disconnect()
                if attempt < CONNECT_ATTEMPTS:
                    await asyncio.sleep(1.0 * attempt)
        else:
            # Re-raise with hint
            if last and ("3221225581" in str(last) or "CredSSP" in str(last)):
                raise ConnectionError(f"{last} — Hint: NLA/CredSSP failed. Try /add with domain e.g. 'TechTally' or check password. FreeRDP me bhi LOGON_FAILURE aaya.") from last
            raise last  # type: ignore[misc]

        self.client = client
        log.info("RDP connected: %s:%s", cfg["rdp_host"], cfg["rdp_port"])
        await self.wait_for_screen()

    async def wait_for_screen(
        self, timeout: float | None = None, min_mean: float = 3.0, min_painted: float = 0.85
    ) -> bool:
        """Pehla screen aane ka intezaar karo (warna screenshot black rehta hai).

        Server Demand Active ke baad desktop tile-by-tile paint karta hai, aur
        pehla bitmap update ke baad bhi zyadaatar screen black rehta hai. Isliye
        tab tak wait karte hain jab tak ~saara screen paint na ho jaye.
        """
        if not self.is_connected:
            return False
        timeout = SCREEN_WAIT_TIMEOUT if timeout is None else timeout
        deadline = asyncio.get_event_loop().time() + timeout
        ready_since: float | None = None
        while asyncio.get_event_loop().time() < deadline:
            img = await self.client.screenshot()
            h = img.convert("L").histogram()
            total = sum(h)
            if total:
                mean = sum(i * c for i, c in enumerate(h)) / total
                painted = (total - h[0]) / total
                if mean >= min_mean and painted >= min_painted:
                    # Paint abhi bhi chal raha ho to thoda aur ruko.
                    if ready_since is None:
                        ready_since = asyncio.get_event_loop().time()
                    elif asyncio.get_event_loop().time() - ready_since >= 0.8:
                        log.info("Screen ready (brightness=%.1f, painted=%.0f%%)", mean, painted * 100)
                        return True
                else:
                    ready_since = None
            await asyncio.sleep(0.4)
        log.warning("Screen %ss me ready nahi hua", timeout)
        return False

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
        # Screen abhi paint nahi hua to thoda ruko (warna photo black aati hai).
        await self.wait_for_screen(timeout=10.0)
        return await self.client.screenshot()

    def status_text(self) -> str:
        if self.is_connected:
            return (
                f"🟢 Connected — {self.cfg['rdp_host']}:{self.cfg['rdp_port']} "
                f"({self.client.width}x{self.client.height})"
            )
        return "🔴 Disconnected"
