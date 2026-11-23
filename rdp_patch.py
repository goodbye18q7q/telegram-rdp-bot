"""simple-rdp ko tolerant banata hai — kuch Windows/RDS servers ke saath.

Do masle fix hote hain:

1. **Licensing exchange** — RDS/Windows Server pe server `LICENSE_REQUEST` (0x01)
   bhejta hai aur client ka jawab (`NEW_LICENSE_REQUEST`) ghunta hai. simple-rdp
   kuch nahi bhejta, to server `Set Error Info` + disconnect kar deta hai.
   Yahan hum jawab bhejte hain (dummy premaster secret — TLS hai, isliye
   licensing-derived keys kaam nahi aati).

2. **Capability exchange** — server kabhi Data PDU (0x17) ya adhoora packet bhejta
   hai Demand Active (0x11) se pehle. simple-rdp turant fail ho jata hai.
   Hum skip karke sahi Demand Active ka wait karte hain.

Ise rdp_manager import karta hai, isliye har process me automatically laga rehta hai.
"""
from __future__ import annotations

import asyncio
import logging
import os
import struct
import time

import simple_rdp.client as _client_module

log = logging.getLogger("rdpbot.patch")

MAX_SKIP = 60
MAX_SECONDS = 25.0
_FLAG = "_rdpbot_tolerant_patched"

SEC_LICENSE_PKT = 0x0080
MSG_LICENSE_REQUEST = 0x01
MSG_ERROR_ALERT = 0xFF
MSG_NEW_LICENSE_REQUEST = 0x13

BB_RANDOM_BLOB = 0x0002
BB_CLIENT_USER_NAME_BLOB = 0x000F
BB_CLIENT_MACHINE_NAME_BLOB = 0x0010
PLATFORM_ID = 0x04010000
KEY_EXCHANGE_ALG_RSA = 0x00000001

_orig_licensing = _client_module.RDPClient._handle_licensing
_orig_capability = _client_module.RDPClient._handle_capability_exchange

_RETRYABLE = (
    "Expected Demand Active PDU",
    "Invalid Demand Active PDU",
)


# ---------------------------------------------------------------------------
# Licensing helpers
# ---------------------------------------------------------------------------

def _blob(btype: int, data: bytes) -> bytes:
    return struct.pack("<HH", btype, len(data)) + data


def _build_new_license_request(username: str, machine: str) -> bytes:
    """Client -> Server NEW_LICENSE_REQUEST (0x13).

    Premaster secret random rakhte hain — session TLS se encrypted hai, isliye
    licensing ke derived keys yahan use nahi hote.
    """
    body = b""
    body += struct.pack("<I", KEY_EXCHANGE_ALG_RSA)      # PreferredKeyExchangeAlg
    body += struct.pack("<I", PLATFORM_ID)               # PlatformId
    body += os.urandom(32)                               # ClientRandom
    body += _blob(BB_RANDOM_BLOB, os.urandom(48))         # EncryptedPremasterSecret
    body += _blob(BB_CLIENT_USER_NAME_BLOB, username.encode() + b"\x00")
    body += _blob(BB_CLIENT_MACHINE_NAME_BLOB, machine.encode() + b"\x00")

    preamble = struct.pack("<BBH", MSG_NEW_LICENSE_REQUEST, 0x83, len(body))
    return struct.pack("<I", SEC_LICENSE_PKT) + preamble + body


def _parse_mcs(response: bytes) -> tuple[int, bytes]:
    """(mcs_header_len, payload) — server ke MCS Send Data Indication se."""
    if len(response) < 7:
        return 0, b""
    if (response[0] >> 2) != 0x1A:
        return 0, response
    return (8 if (response[6] & 0x80) else 7), response[8 if (response[6] & 0x80) else 7:]


async def _handle_licensing(self) -> None:
    """Server licensing PDU padho; LICENSE_REQUEST par jawab bhejo."""
    username = getattr(self, "_username", None) or "username"
    machine = "rdpbot"

    while True:
        try:
            response = await self._recv_x224_data()
        except Exception:
            raise

        header_len, pdu = _parse_mcs(response)
        if header_len == 0 or len(pdu) < 4:
            # licensing nahi hai — agla phase (capability exchange) ke paas bhejo
            self._pending_data = response
            return

        sec_flags = struct.unpack("<I", pdu[:4])[0]
        if not (sec_flags & SEC_LICENSE_PKT):
            self._pending_data = response
            return

        body = pdu[4:]
        if len(body) < 4:
            continue
        msg_type, _flags, size = struct.unpack("<BBH", body[:4])

        if msg_type == MSG_ERROR_ALERT:
            error_code = struct.unpack("<I", body[4:8])[0] if len(body) >= 8 else 0
            if error_code == 0x07:  # STATUS_VALID_CLIENT
                log.info("Licensing completed (valid client)")
                return
            log.info("License error code %#x — agla PDU dekhte hain", error_code)
            continue

        if msg_type == MSG_LICENSE_REQUEST:
            log.info("Server ne LICENSE_REQUEST bheja — NEW_LICENSE_REQUEST bhej rahe hain")
            try:
                out = _build_new_license_request(username, machine)
                await self._send_mcs_data(out, self._io_channel_id)
            except Exception as e:
                log.warning("licensing jawab nahi bheja ja sake: %s", e)
            continue

        # PLATFORM_CHALLENGE aaya to bhi agla PDU dekhte hain
        log.info("Licensing msg_type=%#x — skip", msg_type)


# ---------------------------------------------------------------------------
# Capability exchange
# ---------------------------------------------------------------------------

async def _tolerant_capability_exchange(self) -> None:
    deadline = time.monotonic() + MAX_SECONDS
    for attempt in range(1, MAX_SKIP + 1):
        if time.monotonic() > deadline:
            raise ConnectionError("Timeout waiting for Demand Active PDU")
        try:
            return await _orig_capability(self)
        except ConnectionError as e:
            if not any(marker in str(e) for marker in _RETRYABLE):
                raise  # asli error (auth, network) — retry nahi karni
            log.info(
                "server ne unexpected PDU bheja (%s) — retry %d/%d",
                e,
                attempt,
                MAX_SKIP,
            )
            await asyncio.sleep(0.15)
    raise ConnectionError("Server ne kabhi sahi Demand Active PDU nahi bheja")


def apply() -> None:
    """Patch lagao (idempotent)."""
    cls = _client_module.RDPClient
    if getattr(cls._handle_capability_exchange, _FLAG, False):
        return
    _handle_licensing.__dict__[_FLAG] = True
    cls._handle_licensing = _handle_licensing
    cls._handle_capability_exchange = _tolerant_capability_exchange
    log.info("simple-rdp licensing + capability-exchange patch applied")
