"""/win flow kitni baar kaam karta hai — reliability check."""
from __future__ import annotations

import asyncio
import logging
import sys

from PIL import Image, ImageChops

import commands
import rdp_patch
from config import load_config
from rdp_manager import RDPManager

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
rdp_patch.apply()


def diff(a, b):
    if a.size != b.size:
        b = b.resize(a.size)
    h = ImageChops.difference(a.convert("L"), b.convert("L")).histogram()
    return round(sum(h[40:]) / (a.size[0] * a.size[1]), 3)


async def main():
    hp, user, pw = sys.argv[1], sys.argv[2], sys.argv[3]
    word = sys.argv[4] if len(sys.argv) > 4 else "chrome"
    if ":" in hp:
        host, port = hp.rsplit(":", 1)
        port = int(port)
    else:
        host, port = hp, 3389
    cfg = load_config()
    cfg.update(rdp_host=host, rdp_port=port, rdp_user=user, rdp_pass=pw)
    m = RDPManager(cfg)
    await m.connect()
    for i in range(5):
        base = await m.screenshot()
        await m.execute(commands.a_win_search(word))
        dlg = await m.screenshot()
        r_dialog = diff(base, dlg)
        await m.execute([("key", "escape"), ("wait", 0.8)])
        closed = await m.screenshot()
        r_closed = diff(base, closed)
        print(f"  try{i+1}: dialog={r_dialog} closed_after_esc={r_closed}")
        await asyncio.sleep(1.0)
    await m.disconnect()


asyncio.run(main())
