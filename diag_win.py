"""Diagnostic: /win kaam kar raha hai ya nahi — screenshots ke saath.

Usage: python diag_win.py <host:port> <user> <pass> [word]
"""
from __future__ import annotations

import asyncio
import sys

from PIL import ImageOps

import commands
import rdp_patch
from config import load_config
from rdp_manager import RDPManager

rdp_patch.apply()


def stats(img):
    g = ImageOps.grayscale(img)
    h = g.histogram()
    total = sum(h)
    mean = sum(i * c for i, c in enumerate(h)) / total if total else 0
    return mean


def diff(a, b):
    a = ImageOps.grayscale(a)
    b = ImageOps.grayscale(b)
    if a.size != b.size:
        b = b.resize(a.size)
    h = __import__("PIL.ImageChops", fromlist=["ImageChops"]).difference(a, b).histogram()
    return sum(h[40:]) / (a.size[0] * a.size[1])


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
    print(f"--> connecting {host}:{port} as {user}")
    await m.connect()
    print("--> connected")

    c = m.client
    print(f"--> screen size: {c.width}x{c.height}")

    s0 = await m.screenshot()
    s0.save("diag_0_desktop.png")
    print(f"--> desktop: {s0.width}x{s0.height} mean={stats(s0):.1f}")

    await m.execute(commands.a_win_search(word))
    s1 = await m.screenshot()
    s1.save("diag_1_runbox.png")
    print(f"--> runbox:  {s1.width}x{s1.height} mean={stats(s1):.1f} diff_vs_desktop={diff(s0, s1):.3f}")

    await m.execute([("key", "enter"), ("wait", 2.0)])
    s2 = await m.screenshot()
    s2.save("diag_2_after.png")
    print(f"--> after:   {s2.width}x{s2.height} mean={stats(s2):.1f} diff_vs_desktop={diff(s0, s2):.3f}")
    print(f"--> after vs runbox diff={diff(s1, s2):.3f}")

    await m.disconnect()
    print("--> done")


asyncio.run(main())
