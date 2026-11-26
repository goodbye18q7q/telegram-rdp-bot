"""RDP connection manager — simple-rdp ke RDPClient ka async wrapper."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import socket
import time

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
KEEPALIVE_CHECK_SECONDS = 15.0


def _enable_tcp_keepalive(client) -> bool:
    """Ask the OS to probe an otherwise-idle RDP TCP socket for dead peers."""
    writer = getattr(client, "_tcp_writer", None)
    sock = writer.get_extra_info("socket") if writer else None
    if sock is None:
        return False
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        for option_name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 15), ("TCP_KEEPCNT", 4)):
            option = getattr(socket, option_name, None)
            if option is not None:
                sock.setsockopt(socket.IPPROTO_TCP, option, value)
        return True
    except (AttributeError, OSError, TypeError, ValueError) as exc:
        log.warning("Could not configure RDP TCP keepalive: %s", exc)
        return False


class RDPManager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.client: RDPClient | None = None
        self._exec_lock = asyncio.Lock()
        self._keepalive_task: asyncio.Task | None = None
        self._last_input_at = time.monotonic()
        self._idle_keepalive_seconds = max(0, int(cfg.get("rdp_idle_keepalive_seconds", 120)))

    @property
    def is_connected(self) -> bool:
        return bool(self.client and self.client.is_connected)

    @property
    def keepalive_active(self) -> bool:
        return bool(self.is_connected and self._keepalive_task and not self._keepalive_task.done())

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
        tcp_keepalive = _enable_tcp_keepalive(client)
        self._last_input_at = time.monotonic()
        log.info(
            "RDP connected: %s:%s (TCP keepalive=%s, idle input keepalive=%ss)",
            cfg["rdp_host"], cfg["rdp_port"], tcp_keepalive, self._idle_keepalive_seconds,
        )
        await self.wait_for_screen()
        if self._idle_keepalive_seconds and not (
            self._keepalive_task and not self._keepalive_task.done()
        ):
            self._keepalive_task = asyncio.create_task(self._idle_keepalive_loop())

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

    async def _send_pointer_nudge(self, client) -> None:
        """Move the pointer one pixel and return it, with no click or keystroke."""
        pointer = getattr(getattr(client, "_display", None), "pointer_position", None)
        if pointer is None:
            pointer = (client.width // 2, client.height // 2)
        x, y = int(pointer[0]), int(pointer[1])
        next_x = x + 1 if x < client.width - 1 else max(0, x - 1)
        await client.mouse_move(next_x, y)
        await asyncio.sleep(0.05)
        await client.mouse_move(x, y)
        self._last_input_at = time.monotonic()
        log.debug("Sent RDP keepalive pointer nudge %s -> (%d,%d) -> %s", pointer, next_x, y, pointer)

    async def keepalive_now(self) -> None:
        """Send one manual keepalive nudge immediately."""
        await self.ensure()
        async with self._exec_lock:
            if not self.client or not self.client.is_connected:
                raise ConnectionError("RDP is not connected")
            await self._send_pointer_nudge(self.client)

    async def _idle_keepalive_loop(self) -> None:
        """Keep idle Windows sessions active without clicks or keystrokes.

        After the controller has been idle for the configured interval, move the
        pointer one pixel and return it to its previous position. This real but
        tiny mouse movement is intended to reset Windows' idle-input timer.
        """
        this_task = asyncio.current_task()
        try:
            while self.is_connected:
                await asyncio.sleep(KEEPALIVE_CHECK_SECONDS)
                if not self.is_connected:
                    break
                interval = self._idle_keepalive_seconds
                if not interval or time.monotonic() - self._last_input_at < interval:
                    continue
                async with self._exec_lock:
                    client = self.client
                    if (not client or not client.is_connected or
                            time.monotonic() - self._last_input_at < interval):
                        continue
                    try:
                        await self._send_pointer_nudge(client)
                    except Exception:
                        log.exception("Idle RDP input keepalive failed")
        except asyncio.CancelledError:
            raise
        finally:
            if self._keepalive_task is this_task:
                self._keepalive_task = None

    async def disconnect(self) -> None:
        task, self._keepalive_task = self._keepalive_task, None
        if task and task is not asyncio.current_task() and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
                if action and action[0] != "wait":
                    self._last_input_at = time.monotonic()

    async def screenshot(self):
        """Remote desktop ki current photo (PIL Image)."""
        await self.ensure()
        # Screen ready ka wait — 3s hi kaafi hai, 10s bahut slow lagta hai
        # Black screen aaye to bhi 3s me return (caller /fix se handle karega)
        await self.wait_for_screen(timeout=3.0)
        return await self.client.screenshot()

    async def screenshot_nowait(self):
        """Immediate screenshot without wait — for fast UI refresh"""
        await self.ensure()
        return await self.client.screenshot()

    def status_text(self) -> str:
        if self.is_connected:
            return (
                f"🟢 Connected — {self.cfg['rdp_host']}:{self.cfg['rdp_port']} "
                f"({self.client.width}x{self.client.height})"
            )
        return "🔴 Disconnected"
