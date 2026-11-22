"""Low-level GUI actions jo RDP connection par chalte hain.

Action = tuple:
    ("wait", seconds)
    ("text", "kuch bhi")            -> unicode typing
    ("key", "ctrl+shift+esc")        -> key combination
    ("move", x, y)
    ("click", x, y, button, double)
    ("scroll", delta)               -> +120 up, -120 down (screen center par)
"""
from __future__ import annotations

import asyncio

MODIFIER_ALIASES = {
    "ctrl": "ctrl", "control": "ctrl", "lctrl": "ctrl", "rctrl": "ctrl",
    "shift": "shift", "lshift": "shift", "rshift": "shift",
    "alt": "alt", "lalt": "alt", "ralt": "alt",
    "win": "win", "windows": "win", "lwin": "win", "rwin": "win",
    "meta": "win", "super": "win", "cmd": "win",
}


def parse_combo(combo: str) -> tuple[str, list[str]]:
    """'ctrl+shift+esc' -> ('esc', ['ctrl', 'shift'])"""
    parts = [p.strip().lower() for p in combo.replace(" ", "+").split("+") if p.strip()]
    if not parts:
        raise ValueError("Khali key combination")

    mods: list[str] = []
    key: str | None = None
    for part in parts:
        if part in MODIFIER_ALIASES:
            mod = MODIFIER_ALIASES[part]
            if mod not in mods:
                mods.append(mod)
        else:
            key = part

    if key is None:
        # Sirf modifier diya gaya (e.g. "ctrl") -> use hi key maan lo
        key = mods.pop() if mods else ""

    if not key:
        raise ValueError(f"Key combination samajh nahi aayi: {combo}")
    return key, mods


async def send_combo(client, combo: str) -> None:
    """Modifier hold karke key press karta hai, phir modifiers release."""
    key, mods = parse_combo(combo)
    for mod in mods:
        await client.send_key(mod, mode="hold")
        await asyncio.sleep(0.05)
    await client.send_key(key, mode="press")
    for mod in reversed(mods):
        await client.send_key(mod, mode="release")


async def apply_action(client, action: tuple, cfg: dict) -> None:
    """Ek action ko RDP client par execute karta hai."""
    kind = action[0]

    if kind == "wait":
        await asyncio.sleep(max(0.0, float(action[1])))

    elif kind == "text":
        await client.send_text(str(action[1]), chars_per_second=cfg.get("type_cps", 12.0))

    elif kind == "key":
        await send_combo(client, str(action[1]))

    elif kind == "move":
        await client.mouse_move(int(action[1]), int(action[2]))

    elif kind == "click":
        x, y = int(action[1]), int(action[2])
        button = int(action[3]) if len(action) > 3 else 1
        double = bool(action[4]) if len(action) > 4 else False
        await client.mouse_click(x, y, button=button, double_click=double)

    elif kind == "scroll":
        await client.mouse_wheel(client.width // 2, client.height // 2, int(action[1]))

    else:
        raise ValueError(f"Unknown action: {kind!r}")
