import unittest

from arc import messages as m
from arc import protocol as p


class MessageTests(unittest.TestCase):
    def test_ack_round_trip(self):
        ack = m.Ack(seq=0xABCD)

        self.assertEqual(ack.encode(), bytes.fromhex("abcd"))
        self.assertEqual(m.Ack.decode(ack.encode()), ack)

    def test_video_set_bitrate_round_trip(self):
        msg = m.SetBitrate(bitrate_bps=2_500_000)

        self.assertEqual(msg.encode(), bytes.fromhex("002625a0"))
        self.assertEqual(m.SetBitrate.decode(msg.encode()), msg)

    def test_video_status_report_round_trip(self):
        msg = m.StatusReport(
            state=0x03,
            cpu_temp_c=54,
            cpu_load_pct=42,
            free_disk_mb=1200,
            rssi_dbm=-67,
            tx_frames=300,
            dropped_frames=2,
        )

        self.assertEqual(msg.encode(), bytes.fromhex("03362a04b0bd012c0002"))
        self.assertEqual(m.StatusReport.decode(msg.encode()), msg)

    def test_fc_video_set_layout_round_trip(self):
        msg = m.SetLayout(layout_id=2)

        self.assertEqual(msg.encode(), b"\x02")
        self.assertEqual(m.SetLayout.decode(msg.encode()), msg)

    def test_fc_video_set_source_round_trip(self):
        msg = m.SetSource(slot_id=1, sender_addr=p.ADDR_SENDER_L1)

        self.assertEqual(msg.encode(), bytes((1, p.ADDR_SENDER_L1)))
        self.assertEqual(m.SetSource.decode(msg.encode()), msg)

    def test_fc_video_set_overlay_round_trip(self):
        msg = m.SetOverlay(text="KD3BBP flight")

        self.assertEqual(msg.encode(), b"KD3BBP flight\x00")
        self.assertEqual(m.SetOverlay.decode(msg.encode()), msg)

    def test_fc_video_status_report_round_trip(self):
        msg = m.FcVideoStatusReport(
            slots=(p.ADDR_CONTROLLER, p.ADDR_SENDER_C),
            senders=(
                m.FcVideoSenderStatus(addr=p.ADDR_SENDER_N, flags=m.FC_VIDEO_STATUS_FLAG_ONLINE),
                m.FcVideoSenderStatus(
                    addr=p.ADDR_SENDER_C,
                    flags=(
                        m.FC_VIDEO_STATUS_FLAG_ONLINE
                        | m.FC_VIDEO_STATUS_FLAG_TRANSMITTING
                        | m.FC_VIDEO_STATUS_FLAG_RECORDING
                    ),
                ),
                m.FcVideoSenderStatus(addr=p.ADDR_SENDER_L1, flags=0),
            ),
        )

        encoded = msg.encode()
        # 1 (slot_count) + 2 (slots) + 1 (sender_count) + 6 (3*2) = 10 bytes
        self.assertEqual(
            encoded,
            bytes((
                2, p.ADDR_CONTROLLER, p.ADDR_SENDER_C,
                3,
                p.ADDR_SENDER_N, 0x01,
                p.ADDR_SENDER_C, 0x07,
                p.ADDR_SENDER_L1, 0x00,
            )),
        )
        self.assertEqual(m.FcVideoStatusReport.decode(encoded), msg)

    def test_fc_video_status_report_empty(self):
        msg = m.FcVideoStatusReport(slots=(), senders=())
        self.assertEqual(msg.encode(), b"\x00\x00")
        self.assertEqual(m.FcVideoStatusReport.decode(b"\x00\x00"), msg)

    def test_fc_video_status_report_truncated(self):
        # Header claims 2 slots + 1 sender = 6 bytes; only 5 supplied.
        with self.assertRaises(m.MessageError):
            m.FcVideoStatusReport.decode(bytes((2, 0x10, 0x12, 1, 0x11)))

    def test_fc_video_status_report_rejects_reserved_flag_bits(self):
        msg = m.FcVideoStatusReport(
            slots=(),
            senders=(m.FcVideoSenderStatus(addr=p.ADDR_SENDER_C, flags=0x80),),
        )
        with self.assertRaises(m.MessageError):
            msg.encode()

    def test_radio_set_frequency_round_trip(self):
        msg = m.RadioSetFrequency(frequency_hz=433_920_000)
        self.assertEqual(msg.encode(), bytes.fromhex("19dd1800"))
        self.assertEqual(m.RadioSetFrequency.decode(msg.encode()), msg)

    def test_radio_set_tx_power_round_trip(self):
        msg = m.RadioSetTxPower(tx_power_dbm=-10)
        self.assertEqual(msg.encode(), bytes((0xF6,)))
        self.assertEqual(m.RadioSetTxPower.decode(msg.encode()), msg)

    def test_radio_status_report_round_trip(self):
        msg = m.RadioStatusReport(
            frequency_hz=433_920_000,
            tx_power_dbm=17,
            rssi_dbm=-78,
            snr_db=9,
            error_flags=m.RADIO_ERR_RX_OVERRUN,
            packets_rx=1024,
            packets_tx=256,
        )
        # Same wire layout as the C round-trip vector.
        self.assertEqual(
            msg.encode(),
            bytes.fromhex("19dd1800") + bytes((0x11, 0xB2, 0x09, 0x08))
            + bytes.fromhex("0400") + bytes.fromhex("0100"),
        )
        self.assertEqual(m.RadioStatusReport.decode(msg.encode()), msg)

    def test_power_set_output_round_trip(self):
        msg = m.PowerSetOutput(channel=3, state=m.POWER_ON)
        self.assertEqual(msg.encode(), bytes((3, 1)))
        self.assertEqual(m.PowerSetOutput.decode(msg.encode()), msg)

    def test_power_set_output_rejects_invalid_state(self):
        for bad in (0x42, m.POWER_ON | m.POWER_CHAN_FAULT_OVERCURRENT):
            with self.subTest(state=bad):
                with self.assertRaises(m.MessageError):
                    m.PowerSetOutput(channel=0, state=bad).encode()

    def test_power_set_output_mask_round_trip(self):
        msg = m.PowerSetOutputMask(enable_mask=0x15, state_mask=0x11)
        self.assertEqual(msg.encode(), bytes((0x15, 0x11)))
        self.assertEqual(m.PowerSetOutputMask.decode(msg.encode()), msg)

    def test_power_status_report_six_channels_round_trip(self):
        msg = m.PowerStatusReport(
            channels=(
                m.PowerChannelStatus(state=m.POWER_ON,  current_ma=500),
                m.PowerChannelStatus(state=m.POWER_OFF, current_ma=0),
                m.PowerChannelStatus(state=m.POWER_ON,  current_ma=1500),
                m.PowerChannelStatus(
                    state=m.POWER_ON | m.POWER_CHAN_FAULT_OVERCURRENT,
                    current_ma=3100,
                ),
                m.PowerChannelStatus(state=m.POWER_OFF, current_ma=0),
                m.PowerChannelStatus(state=m.POWER_ON,  current_ma=220),
            ),
            bus_voltage_mv=5012,
            temp_c=41,
        )
        encoded = msg.encode()
        self.assertEqual(len(encoded), 22)
        self.assertEqual(encoded[0], 6)
        # Channel 3: state byte has overcurrent bit, current 3100 = 0x0C1C
        self.assertEqual(encoded[1 + 3 * 3], 0x41)
        self.assertEqual(encoded[1 + 3 * 3 + 1 : 1 + 3 * 3 + 3], bytes.fromhex("0c1c"))
        # bus voltage at byte 19 (1 + 6*3): 5012 = 0x1394
        self.assertEqual(encoded[19:21], bytes.fromhex("1394"))
        self.assertEqual(encoded[21], 41)
        self.assertEqual(m.PowerStatusReport.decode(encoded), msg)

    def test_power_status_report_empty(self):
        msg = m.PowerStatusReport(channels=(), bus_voltage_mv=0, temp_c=-10)
        self.assertEqual(msg.encode(), bytes.fromhex("00") + bytes.fromhex("0000") + bytes((0xF6,)))
        self.assertEqual(m.PowerStatusReport.decode(msg.encode()), msg)

    def test_power_status_report_truncated(self):
        # channel_count = 2 -> need 1 + 6 + 2 + 1 = 10 bytes, supply 9.
        with self.assertRaises(m.MessageError):
            m.PowerStatusReport.decode(bytes((2, 0x01, 0x00, 0xC8, 0x00, 0x00, 0x00, 0x13, 0x88)))

    def test_decode_frame_payload_for_radio_and_power(self):
        radio_frame = p.Frame(
            src=p.ADDR_FC_N,
            dst=p.ADDR_RADIO_R,
            flags=p.FLAG_RELIABLE,
            session=1,
            seq=4,
            family=p.FAMILY_RADIO,
            type=m.RadioType.SET_FREQUENCY,
            payload=m.RadioSetFrequency(433_920_000).encode(),
        )
        power_frame = p.Frame(
            src=p.ADDR_FC_N,
            dst=p.ADDR_ARCH_MEGA_N,
            flags=p.FLAG_RELIABLE,
            session=1,
            seq=5,
            family=p.FAMILY_POWER,
            type=m.PowerType.SET_OUTPUT,
            payload=m.PowerSetOutput(2, m.POWER_ON).encode(),
        )
        self.assertEqual(
            m.decode_frame_payload(radio_frame),
            m.RadioSetFrequency(433_920_000),
        )
        self.assertEqual(
            m.decode_frame_payload(power_frame),
            m.PowerSetOutput(2, m.POWER_ON),
        )

    def test_empty_control_messages_reject_payloads(self):
        with self.assertRaises(m.MessageError):
            m.decode_video(m.VideoType.START_STREAM, b"nope")

        with self.assertRaises(m.MessageError):
            m.decode_fc_video(m.FcVideoType.GET_STATUS, b"nope")

    def test_invalid_lengths_rejected(self):
        cases = [
            (m.Ack.decode, b"\x00"),
            (m.SetBitrate.decode, b"\x00\x01"),
            (m.StatusReport.decode, bytes(9)),
            (m.SetLayout.decode, b""),
            (m.SetSource.decode, b"\x01"),
        ]
        for decoder, payload in cases:
            with self.subTest(decoder=decoder.__qualname__):
                with self.assertRaises(m.MessageError):
                    decoder(payload)

    def test_numeric_ranges_rejected(self):
        cases = [
            lambda: m.Ack(0x10000).encode(),
            lambda: m.SetBitrate(-1).encode(),
            lambda: m.StatusReport(0, 0, 0, 0, -129, 0, 0).encode(),
            lambda: m.StatusReport(0, 101, 0, 0x10000, 0, 0, 0).encode(),
            lambda: m.SetLayout(0x100).encode(),
            lambda: m.SetSource(0, 0x100).encode(),
        ]
        for case in cases:
            with self.subTest(case=case):
                with self.assertRaises(m.MessageError):
                    case()

    def test_overlay_validation(self):
        with self.assertRaises(m.MessageError):
            m.SetOverlay.decode(b"KD3BBP")

        with self.assertRaises(m.MessageError):
            m.SetOverlay.decode(b"\xff\x00")

        too_long = "x" * p.MAX_PAYLOAD_SIZE
        with self.assertRaises(m.MessageError):
            m.SetOverlay(too_long).encode()

    def test_decode_frame_payload_for_known_families(self):
        video = p.Frame(
            src=p.ADDR_CONTROLLER,
            dst=p.ADDR_SENDER_C,
            flags=p.FLAG_RELIABLE,
            session=1,
            seq=1,
            family=p.FAMILY_VIDEO,
            type=m.VideoType.SET_BITRATE,
            payload=m.SetBitrate(1_000_000).encode(),
        )
        fc_video = p.Frame(
            src=p.ADDR_FC_N,
            dst=p.ADDR_CONTROLLER,
            flags=p.FLAG_RELIABLE,
            session=1,
            seq=2,
            family=p.FAMILY_FC_VIDEO,
            type=m.FcVideoType.SET_SOURCE,
            payload=m.SetSource(1, p.ADDR_SENDER_C).encode(),
        )
        fc_coord = p.Frame(
            src=p.ADDR_FC_C,
            dst=p.ADDR_FC_N,
            flags=0,
            session=1,
            seq=3,
            family=p.FAMILY_FC_COORD,
            type=0x99,
            payload=b"opaque",
        )

        self.assertEqual(m.decode_frame_payload(video), m.SetBitrate(1_000_000))
        self.assertEqual(m.decode_frame_payload(fc_video), m.SetSource(1, p.ADDR_SENDER_C))
        self.assertEqual(m.decode_frame_payload(fc_coord), b"opaque")

    def test_unknown_type_or_family_rejected(self):
        with self.assertRaises(ValueError):
            m.decode_video(0xFF, b"")

        frame = p.Frame(
            src=1,
            dst=2,
            flags=0,
            session=1,
            seq=1,
            family=0xFE,
            type=0,
            payload=b"",
        )
        with self.assertRaises(m.MessageError):
            m.decode_frame_payload(frame)


if __name__ == "__main__":
    unittest.main()
