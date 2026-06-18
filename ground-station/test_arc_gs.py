from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import unittest


def load_arc_gs():
    path = Path(__file__).with_name("arc_gs.py")
    spec = importlib.util.spec_from_file_location("arc_gs", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


arc_gs = load_arc_gs()
protocol = arc_gs.protocol
messages = arc_gs.messages


class FakeClient:
    def __init__(self):
        self.writes = []

    async def write_gatt_char(self, char_uuid, data, response=False):
        self.writes.append((char_uuid, bytes(data), response))


class GroundStationProtocolTests(unittest.IsolatedAsyncioTestCase):
    async def test_decodes_chunked_heartbeat(self):
        client = FakeClient()
        gs = arc_gs.GroundStation(client)
        frame = protocol.build_frame(
            protocol.ADDR_RADIO_CMD,
            protocol.ADDR_GROUND,
            0,
            1,
            10,
            protocol.FAMILY_NETMGMT,
            protocol.NETMGMT_HEARTBEAT,
        )
        encoded = protocol.cobs_encode(frame)

        gs.on_notify(None, encoded[:3])
        self.assertEqual(gs.heartbeat_count, 0)
        gs.on_notify(None, encoded[3:])

        self.assertEqual(gs.heartbeat_count, 1)
        heartbeat = await gs.wait_for_heartbeat(0.01)
        self.assertEqual(heartbeat.src, protocol.ADDR_RADIO_CMD)

    async def test_send_freq_writes_reliable_radio_command(self):
        client = FakeClient()
        gs = arc_gs.GroundStation(client)

        seq = await gs.send_freq(915.5)

        self.assertEqual(seq, 0)
        self.assertEqual(len(client.writes), 1)
        char_uuid, encoded, response = client.writes[0]
        self.assertEqual(char_uuid, arc_gs.NUS_RX_WRITE)
        self.assertFalse(response)

        frame = protocol.decode_frame(encoded)
        self.assertEqual(frame.src, protocol.ADDR_GROUND)
        self.assertEqual(frame.dst, protocol.ADDR_RADIO_CMD)
        self.assertEqual(frame.flags, protocol.FLAG_RELIABLE)
        self.assertEqual(frame.family, protocol.FAMILY_RADIO)
        self.assertEqual(frame.type, int(messages.RadioType.SET_FREQUENCY))
        self.assertEqual(int.from_bytes(frame.payload, "big"), 915_500_000)

    async def test_send_phy_profile_writes_reliable_radio_command(self):
        client = FakeClient()
        gs = arc_gs.GroundStation(client)

        seq = await gs.send_phy_profile(messages.RADIO_PHY_PROFILE_FAST_BW500)

        self.assertEqual(seq, 0)
        self.assertEqual(len(client.writes), 1)
        _, encoded, response = client.writes[0]
        self.assertFalse(response)

        frame = protocol.decode_frame(encoded)
        self.assertEqual(frame.src, protocol.ADDR_GROUND)
        self.assertEqual(frame.dst, protocol.ADDR_RADIO_CMD)
        self.assertEqual(frame.flags, protocol.FLAG_RELIABLE)
        self.assertEqual(frame.family, protocol.FAMILY_RADIO)
        self.assertEqual(frame.type, int(messages.RadioType.SET_PHY_PROFILE))
        self.assertEqual(frame.payload, bytes((messages.RADIO_PHY_PROFILE_FAST_BW500,)))

    async def test_ping_writes_reliable_netmgmt_heartbeat(self):
        client = FakeClient()
        gs = arc_gs.GroundStation(client)

        seq = await gs._send_frame(
            protocol.ADDR_TEENSY_HUB,
            protocol.FAMILY_NETMGMT,
            protocol.NETMGMT_HEARTBEAT,
            reliable=True,
        )

        self.assertEqual(seq, 0)
        self.assertEqual(len(client.writes), 1)
        _, encoded, response = client.writes[0]
        self.assertFalse(response)

        frame = protocol.decode_frame(encoded)
        self.assertEqual(frame.src, protocol.ADDR_GROUND)
        self.assertEqual(frame.dst, protocol.ADDR_TEENSY_HUB)
        self.assertEqual(frame.flags, protocol.FLAG_RELIABLE)
        self.assertEqual(frame.family, protocol.FAMILY_NETMGMT)
        self.assertEqual(frame.type, protocol.NETMGMT_HEARTBEAT)
        self.assertEqual(frame.payload, b"")

    async def test_ping_resolves_on_matching_ack(self):
        client = FakeClient()
        gs = arc_gs.GroundStation(client)

        task = asyncio.create_task(gs.ping(protocol.ADDR_TEENSY_HUB, 0.5))
        await asyncio.sleep(0)
        self.assertEqual(len(client.writes), 1)
        sent = protocol.decode_frame(client.writes[0][1])

        ack_payload = sent.seq.to_bytes(2, "big")
        ack = protocol.build_frame(
            protocol.ADDR_TEENSY_HUB,
            protocol.ADDR_GROUND,
            protocol.FLAG_ACK,
            sent.session,
            99,
            protocol.FAMILY_NETMGMT,
            protocol.NETMGMT_ACK,
            ack_payload,
        )
        gs.on_notify(None, protocol.cobs_encode(ack))

        frame, rtt_ms = await task
        self.assertEqual(frame.src, protocol.ADDR_TEENSY_HUB)
        self.assertGreaterEqual(rtt_ms, 0.0)

    async def test_reliable_downlink_is_acknowledged(self):
        client = FakeClient()
        gs = arc_gs.GroundStation(client)
        frame = protocol.build_frame(
            protocol.ADDR_CONTROLLER,
            protocol.ADDR_GROUND,
            protocol.FLAG_RELIABLE,
            7,
            42,
            protocol.FAMILY_FC_VIDEO,
            int(messages.FcVideoType.STATUS_REPORT),
            b"not-valid-status",
        )

        gs.on_notify(None, protocol.cobs_encode(frame))
        await asyncio.sleep(0)

        self.assertEqual(len(client.writes), 1)
        ack = protocol.decode_frame(client.writes[0][1])
        self.assertEqual(ack.src, protocol.ADDR_GROUND)
        self.assertEqual(ack.dst, protocol.ADDR_CONTROLLER)
        self.assertEqual(ack.flags, protocol.FLAG_ACK)
        self.assertEqual(ack.family, protocol.FAMILY_NETMGMT)
        self.assertEqual(ack.type, protocol.NETMGMT_ACK)
        self.assertEqual(ack.payload, (42).to_bytes(2, "big"))

    async def test_wait_for_ack_resolves_matching_sequence(self):
        client = FakeClient()
        gs = arc_gs.GroundStation(client)
        waiter = asyncio.create_task(gs.wait_for_ack(42, 0.5))

        ack_payload = (42).to_bytes(2, "big")
        frame = protocol.build_frame(
            protocol.ADDR_RADIO_CMD,
            protocol.ADDR_GROUND,
            protocol.FLAG_ACK,
            2,
            99,
            protocol.FAMILY_NETMGMT,
            protocol.NETMGMT_ACK,
            ack_payload,
        )
        gs.on_notify(None, protocol.cobs_encode(frame))

        ack = await waiter
        self.assertEqual(ack.src, protocol.ADDR_RADIO_CMD)
        self.assertEqual(ack.payload, ack_payload)

    async def test_status_command_requests_controller_status(self):
        client = FakeClient()
        gs = arc_gs.GroundStation(client)

        seq = await gs.request_status()

        self.assertEqual(seq, 0)
        frame = protocol.decode_frame(client.writes[0][1])
        self.assertEqual(frame.src, protocol.ADDR_GROUND)
        self.assertEqual(frame.dst, protocol.ADDR_CONTROLLER)
        self.assertEqual(frame.flags, protocol.FLAG_RELIABLE)
        self.assertEqual(frame.family, protocol.FAMILY_FC_VIDEO)
        self.assertEqual(frame.type, int(messages.FcVideoType.GET_STATUS))
        self.assertEqual(frame.payload, b"")

    def test_parse_addr_accepts_aliases_and_numbers(self):
        self.assertEqual(arc_gs.parse_addr("hub"), protocol.ADDR_TEENSY_HUB)
        self.assertEqual(arc_gs.parse_addr("radio-cmd"), protocol.ADDR_RADIO_CMD)
        self.assertEqual(arc_gs.parse_addr("0x20"), protocol.ADDR_RADIO_CMD)

    async def test_describes_flight_telemetry(self):
        msg = messages.FlightTelemetry(
            time_ms=1,
            stage=messages.FC_COORD_STAGE_BOOST,
            accel_x_mg=0,
            accel_y_mg=0,
            accel_z_mg=1000,
            vel_x_cms=0,
            vel_y_cms=0,
            vel_z_cms=1234,
            lat_e7=391234567,
            lon_e7=-1049876543,
            alt_cm=185000,
            temp_cdeg=2300,
            voltage_mv=11900,
            gps_fix_quality=messages.FC_COORD_GPS_FIX_3D,
            roll_cdeg=25,
            pitch_cdeg=-120,
            yaw_cdeg=9012,
        )
        frame = protocol.Frame(
            src=protocol.ADDR_FC_N,
            dst=protocol.ADDR_GROUND,
            flags=0,
            session=1,
            seq=7,
            family=protocol.FAMILY_FC_COORD,
            type=messages.FcCoordType.FLIGHT_TELEMETRY,
            payload=msg.encode(),
        )

        detail = arc_gs.describe_payload(frame)

        self.assertIsNotNone(detail)
        self.assertIn("FC_TELEM", detail)
        self.assertIn("stage=boost", detail)
        self.assertIn("alt=1850.0m", detail)
        self.assertIn("vbat=11.90V", detail)

    def test_describes_rich_fc_video_status(self):
        status = arc_gs.ControllerVideoStatus(
            layout="split",
            desired_sources=(protocol.ADDR_CONTROLLER, protocol.ADDR_SENDER_AIRBRAKE),
            active_sources=(protocol.ADDR_CONTROLLER, protocol.ADDR_UNASSIGNED),
            senders=(
                arc_gs.SenderVideoSnapshot(
                    addr=protocol.ADDR_SENDER_AIRBRAKE,
                    flags=messages.FC_VIDEO_STATUS_FLAG_ONLINE
                    | messages.FC_VIDEO_STATUS_FLAG_RECORDING,
                    status=messages.StatusReport(
                        state=1,
                        cpu_temp_c=52,
                        cpu_load_pct=33,
                        free_disk_mb=1234,
                        rssi_dbm=-42,
                        tx_frames=9,
                        dropped_frames=1,
                    ),
                    name="airbrake-cam",
                ),
                arc_gs.SenderVideoSnapshot(
                    addr=protocol.ADDR_SENDER_PAYLOAD,
                    flags=0,
                    status=None,
                ),
            ),
        )
        frame = protocol.Frame(
            src=protocol.ADDR_CONTROLLER,
            dst=protocol.ADDR_GROUND,
            flags=protocol.FLAG_RELIABLE,
            session=1,
            seq=7,
            family=protocol.FAMILY_FC_VIDEO,
            type=int(messages.FcVideoType.STATUS_REPORT),
            payload=status.encode(),
        )

        detail = arc_gs.describe_payload(frame)

        self.assertIsNotNone(detail)
        self.assertIn("layout=split", detail)
        self.assertIn("desired=slot0=Pi Controller", detail)
        self.assertIn("active=slot0=Pi Controller", detail)
        self.assertIn("connected=Airbrake Sender", detail)
        # Discovered friendly name is shown beside the address.
        self.assertIn("Airbrake Sender (0x12) airbrake-cam", detail)
        self.assertIn("Payload Sender (0x13) | offline | video=no-report", detail)


if __name__ == "__main__":
    unittest.main()
