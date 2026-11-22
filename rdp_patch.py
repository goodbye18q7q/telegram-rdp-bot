"""simple-rdp ko thoda tolerant banata hai — "Expected Demand Active PDU" fix.

Kuch RDP servers (RD Gateway, xrdp, kuch Windows builds) licensing ke baad
pehle ek Data PDU (0x17) bhej dete hain, jabki client Demand Active PDU (0x11)
expect karta hai. simple-rdp 0.8.10 turant `ConnectionError` raise kar deta hai:

    ConnectionError: Expected Demand Active PDU, got type 0x17

Ye patch wahi method wrap karta hai: jab tak server Demand Active na bheje,
PDU ko skip karte hain (max MAX_SKIP attempts), phir normal flow continue hota hai.

Ise rdp_manager import karta hai, isliye har process me automatically laga rehta hai.
"""
from __future__ import annotations

import logging

import simple_rdp.client as _client_module

log = logging.getLogger("rdpbot.patch")

MAX_SKIP = 20
_FLAG = "_rdpbot_tolerant_patched"

_original = _client_module.RDPClient._handle_capability_exchange


async def _tolerant_capability_exchange(self) -> None:
    last: Exception | None = None
    for attempt in range(1, MAX_SKIP + 1):
        try:
            return await _original(self)
        except ConnectionError as e:
            if "Expected Demand Active" not in str(e):
                raise
            last = e
            log.info(
                "server ne unexpected PDU bheja (%s) — retry %d/%d",
                e,
                attempt,
                MAX_SKIP,
            )
    raise ConnectionError(f"Server ne kabhi Demand Active PDU nahi bheja: {last}")


def apply() -> None:
    """Patch lagao (idempotent)."""
    if getattr(_client_module.RDPClient._handle_capability_exchange, _FLAG, False):
        return
    _tolerant_capability_exchange.__dict__[_FLAG] = True
    _client_module.RDPClient._handle_capability_exchange = _tolerant_capability_exchange
    log.info("simple-rdp capability-exchange patch applied")
