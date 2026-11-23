"""simple-rdp ko tolerant banata hai — "Expected Demand Active PDU" / "Invalid Demand Active PDU" fix.

Kuch RDP servers (RD Gateway, xrdp, kuch Windows builds) licensing ke baad
Demand Active PDU (0x11) se pehle ek Data PDU (0x17) bhej dete hain, ya kabhi
kabhi adhoora/garbage packet. simple-rdp 0.8.10 turant fail ho jata hai:

    ConnectionError: Expected Demand Active PDU, got type 0x17
    ConnectionError: Invalid Demand Active PDU

Ye patch wahi method wrap karta hai: jab tak server sahi Demand Active na bheje,
PDU ko skip karte hain (max MAX_SKIP attempts, max MAX_SECONDS time), phir normal
flow continue hota hai. Auth failure jaise asli errors ko propagate karte hain.

Ise rdp_manager import karta hai, isliye har process me automatically laga rehta hai.
"""
from __future__ import annotations

import asyncio
import logging
import time

import simple_rdp.client as _client_module

log = logging.getLogger("rdpbot.patch")

MAX_SKIP = 60
MAX_SECONDS = 25.0
_FLAG = "_rdpbot_tolerant_patched"

_original = _client_module.RDPClient._handle_capability_exchange

_RETRYABLE = (
    "Expected Demand Active PDU",
    "Invalid Demand Active PDU",
)


async def _tolerant_capability_exchange(self) -> None:
    deadline = time.monotonic() + MAX_SECONDS
    for attempt in range(1, MAX_SKIP + 1):
        if time.monotonic() > deadline:
            raise ConnectionError("Timeout waiting for Demand Active PDU")
        try:
            return await _original(self)
        except ConnectionError as e:
            if not any(marker in str(e) for marker in _RETRYABLE):
                raise  # asli error (auth, network) — retry nahi karni
            log.info(
                "server ne unexpected PDU bheja (%s) — retry %d/%d",
                e,
                attempt,
                MAX_SKIP,
            )
            # Thoda ruko taaki server agla PDU bhej sake
            await asyncio.sleep(0.15)
    raise ConnectionError("Server ne kabhi sahi Demand Active PDU nahi bheja")


def apply() -> None:
    """Patch lagao (idempotent)."""
    if getattr(_client_module.RDPClient._handle_capability_exchange, _FLAG, False):
        return
    _tolerant_capability_exchange.__dict__[_FLAG] = True
    _client_module.RDPClient._handle_capability_exchange = _tolerant_capability_exchange
    log.info("simple-rdp capability-exchange patch applied")
