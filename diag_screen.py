"""Screen update test — connect, wait, screenshot, check pipeline stats."""
from __future__ import annotations

import asyncio
import logging
import sys

import rdp_patch
from config import load_config
from rdp_manager import RDPManager

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
rdp_patch.apply()


def brightness(img):
    from PIL import ImageOps
    h = ImageOps.grayscale(img).histogram()
    t = sum(h)
    return sum(i * c for i, c in enumerate(h)) / t if t else 0


async def main():
    hp, user, pw = sys.argv[1], sys.argv[2], sys.argv[3]
    if ":" in hp:
        host, port = hp.rsplit(":", 1)
        port = int(port)
    else:
        host, port = hp, 3389
    cfg = load_config()
    cfg.update(rdp_host=host, rdp_port=port, rdp_user=user, rdp_pass=pw)

    m = RDPManager(cfg)
    await m.connect()
    print("connected", m.client.width, m.client.height)

    d = m.client._display
    for i in range(8):
        await asyncio.sleep(1.5)
        img = await m.screenshot()
        st = d.get_statistics() if hasattr(d, "get_statistics") else {}
        raw = d._raw_display_image
        print(
            f"t={i*1.5:.1f}s bright={brightness(img):.1f} raw={'None' if raw is None else raw.size} "
            f"stats={ {k: st[k] for k in list(st)[:6]} }"
        )
        img.save(f"scr_{i}.png")

    await m.disconnect()


asyncio.run(main())
