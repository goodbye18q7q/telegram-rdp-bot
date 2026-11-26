from __future__ import annotations

import asyncio
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import rdp_manager


class FakeSocket:
    def __init__(self):
        self.options = []

    def setsockopt(self, level, option, value):
        self.options.append((level, option, value))


class FakeClient:
    def __init__(self, *, connected=True, pointer=(123, 456)):
        self.is_connected = connected
        self.width = 1366
        self.height = 768
        self._display = SimpleNamespace(pointer_position=pointer)
        self._tcp_writer = SimpleNamespace(get_extra_info=lambda name: self.sock)
        self.sock = FakeSocket()
        self.mouse_move = AsyncMock()


class TestRdpKeepalive(unittest.TestCase):
    def test_enables_tcp_keepalive_socket_options(self):
        client = FakeClient()
        self.assertTrue(rdp_manager._enable_tcp_keepalive(client))
        self.assertIn((rdp_manager.socket.SOL_SOCKET, rdp_manager.socket.SO_KEEPALIVE, 1), client.sock.options)
        if hasattr(rdp_manager.socket, "TCP_KEEPIDLE"):
            self.assertIn((rdp_manager.socket.IPPROTO_TCP, rdp_manager.socket.TCP_KEEPIDLE, 60), client.sock.options)

    def test_idle_keepalive_moves_at_current_position_without_click_or_key(self):
        async def run():
            mgr = rdp_manager.RDPManager({"rdp_host": "host", "rdp_port": 3389,
                                          "rdp_idle_keepalive_seconds": 1})
            client = FakeClient()
            mgr.client = client
            mgr._last_input_at = time.monotonic() - 10
            with patch.object(rdp_manager, "KEEPALIVE_CHECK_SECONDS", 0.005):
                task = asyncio.create_task(mgr._idle_keepalive_loop())
                await asyncio.sleep(0.02)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            client.mouse_move.assert_awaited_with(123, 456)
            self.assertIsNone(mgr._keepalive_task)

        asyncio.run(run())

    def test_recent_controller_activity_suppresses_idle_keepalive(self):
        async def run():
            mgr = rdp_manager.RDPManager({"rdp_host": "host", "rdp_port": 3389,
                                          "rdp_idle_keepalive_seconds": 120})
            client = FakeClient()
            mgr.client = client
            mgr._last_input_at = time.monotonic()
            with patch.object(rdp_manager, "KEEPALIVE_CHECK_SECONDS", 0.005):
                task = asyncio.create_task(mgr._idle_keepalive_loop())
                await asyncio.sleep(0.02)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            client.mouse_move.assert_not_awaited()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
