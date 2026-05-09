"""End-to-end test of Controller <-> Sender control plane.

Uses real localhost TCP (no UART) to exercise the entire stack:
framing, routing, reliability, heartbeat, and status report flow.
"""

import asyncio
import unittest
from contextlib import suppress

from arc import messages, protocol as p
from arc.controller import Controller
from arc.runtime import QueuedTcpLink, TcpServer, run_tick_loop
from arc.sender import Sender


class IntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_controller_commands_sender_and_receives_status(self):
        controller = Controller(
            sender_addrs=(p.ADDR_SENDER_C,),
            session=10,
            heartbeat_interval_s=10.0,  # avoid noise in this test
            peer_timeout_s=10.0,
        )
        sender = Sender(
            addr=p.ADDR_SENDER_C,
            paired_fc=None,
            controller_addr=p.ADDR_CONTROLLER,
            session=20,
            heartbeat_interval_s=10.0,
            peer_timeout_s=10.0,
        )

        controller_to_sender = QueuedTcpLink(
            lambda f: controller.receive(f, _loop_now())
        )
        controller.set_links({"sender-c": controller_to_sender})

        sender_to_controller = QueuedTcpLink(
            lambda f: sender.receive(f, _loop_now())
        )
        sender.set_links({"controller": sender_to_controller})

        ip_to_link = {"127.0.0.1": controller_to_sender}
        server = TcpServer("127.0.0.1", 0, ip_to_link.get)
        await server.start()
        port = server._server.sockets[0].getsockname()[1]

        stop_event = asyncio.Event()
        client_task = asyncio.create_task(
            sender_to_controller.run_client("127.0.0.1", port)
        )
        controller_tick = asyncio.create_task(
            run_tick_loop(controller.tick, interval_s=0.01, stop_event=stop_event)
        )
        sender_tick = asyncio.create_task(
            run_tick_loop(sender.tick, interval_s=0.01, stop_event=stop_event)
        )

        try:
            await _wait_until(
                lambda: controller_to_sender.online and sender_to_controller.online
            )

            controller.start_sender(p.ADDR_SENDER_C, now=_loop_now())
            await _wait_until(lambda: sender.transmitting)
            await _wait_until(
                lambda: controller.node.reliable.pending_count == 0
            )

            report = messages.StatusReport(
                state=0x03,
                cpu_temp_c=42,
                cpu_load_pct=18,
                free_disk_mb=2048,
                rssi_dbm=-60,
                tx_frames=10,
                dropped_frames=0,
            )
            sender.report_status(report, now=_loop_now())
            await _wait_until(
                lambda: controller.sender(p.ADDR_SENDER_C).last_status is not None
            )
            self.assertEqual(
                controller.sender(p.ADDR_SENDER_C).last_status.report, report
            )
            self.assertTrue(controller.health.is_online(p.ADDR_SENDER_C))
            self.assertTrue(sender.health.is_online(p.ADDR_CONTROLLER))
        finally:
            stop_event.set()
            await sender_to_controller.stop()
            await controller_to_sender.stop()
            for t in (client_task, controller_tick, sender_tick):
                t.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await t
            await server.stop()


def _loop_now() -> float:
    return asyncio.get_running_loop().time()


async def _wait_until(predicate, timeout=2.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting for condition")


if __name__ == "__main__":
    unittest.main()
