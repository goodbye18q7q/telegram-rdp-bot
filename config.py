"""Configuration — sab kuch environment variables se aata hai."""
from __future__ import annotations

import os


def _int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
    except ValueError:
        return default


def load_config() -> dict:
    allowed = [
        int(x.strip())
        for x in os.environ.get("ALLOWED_USERS", "").split(",")
        if x.strip().isdigit()
    ]

    return {
        # Telegram
        "telegram_token": os.environ.get("TELEGRAM_TOKEN", "").strip(),
        # Comma-separated Telegram user IDs. Khaali = sab allow (warning hoti hai).
        "allowed_users": allowed,
        # RDP (Windows machine)
        "rdp_host": os.environ.get("RDP_HOST", "").strip(),
        "rdp_port": _int("RDP_PORT", 3389),
        "rdp_user": os.environ.get("RDP_USER", "").strip(),
        "rdp_pass": os.environ.get("RDP_PASS", ""),
        "rdp_domain": os.environ.get("RDP_DOMAIN", "").strip(),
        # Screen size aur performance
        "rdp_width": _int("RDP_WIDTH", 1366),
        "rdp_height": _int("RDP_HEIGHT", 768),
        "capture_fps": _int("CAPTURE_FPS", 5),
        # Typing speed (chars/sec)
        "type_cps": _float("TYPE_CPS", 12.0),
    }
