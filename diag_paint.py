"""Screen paint kaise progress karta hai — mean/painted har second."""
from __future__ import annotations

import asyncio
import logging
import sys

import rdp_patch
from config import load_config
from rdp_manager import RDPManager

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
rdp_patch.apply()


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
    t0 = asyncio.get_event_loop().time()
    await m.connect()
    print(f"connect() took {asyncio.get_event_loop().time()-t0:.1f}s")
    for i in range(14):
        await asyncio.sleep(1.0)
        img = await m.screenshot()
        h = img.convert("L").histogram()
        total = sum(h)
        mean = sum(j * c for j, c in enumerate(h)) / total if total else 0
        painted = (total - h[0]) / total if total else 0
        st = m.client.display._stats
        print(f"t={i+1}s mean={mean:6.1f} painted={painted*100:5.1f}% bitmaps={st.get('bitmaps_applied')}")
    await m.disconnect()


asyncio.run(main())
