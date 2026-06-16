import unittest

from arc_protocol import protocol as p


VECTORS = [
    {
        "name": "empty_zero",
        "src": 0x00,
        "dst": 0x00,
        "flags": 0x00,
        "session": 0,
        "seq": 0,
        "family": 0x00,
        "type": 0x00,
        "payload": "",
        "frame": "0a0000000000000000f7ea",
        "encoded": "020a0101010101010103f7ea00",
    },
    {
        "name": "heartbeat",
        "src": 0x03,
        "dst": 0x02,
        "flags": 0x00,
        "session": 1,
        "seq": 42,
        "family": 0x00,
        "type": 0x01,
        "payload": "",
        "frame": "0a03020001002a0001a40b",
        "encoded": "040a03020201022a0401a40b00",
    },
    {
        "name": "small_command",
        "src": 0x01,
        "dst": 0x04,
        "flags": 0x01,
        "session": 5,
        "seq": 0x1234,
        "family": 0x01,
        "type": 0x42,
        "payload": "102030",
        "frame": "0d010401051234014210203063d7",
        "encoded": "0f0d010401051234014210203063d700",
    },
    {
        "name": "payload_with_zeros",
        "src": 0x04,
        "dst": 0x01,
        "flags": 0x00,
        "session": 7,
        "seq": 100,
        "family": 0x01,
        "type": 0x01,
        "payload": "00ff00aa00",
        "frame": "0f040100070064010100ff00aa00ee27",
        "encoded": "040f040102070464010102ff02aa03ee2700",
    },
    {
        "name": "max_payload",
        "src": 0x02,
        "dst": 0x01,
        "flags": 0x03,
        "session": 255,
        "seq": 0xFFFF,
        "family": 0x01,
        "type": 0xFF,
        "payload": "".join(f"{i:02x}" for i in range(p.MAX_PAYLOAD_SIZE)),
        "frame": (
            "fb020103ffffff01ff"
            "000102030405060708090a0b0c0d0e0f"
            "101112131415161718191a1b1c1d1e1f"
            "202122232425262728292a2b2c2d2e2f"
            "303132333435363738393a3b3c3d3e3f"
            "404142434445464748494a4b4c4d4e4f"
            "505152535455565758595a5b5c5d5e5f"
            "606162636465666768696a6b6c6d6e6f"
            "707172737475767778797a7b7c7d7e7f"
            "808182838485868788898a8b8c8d8e8f"
            "909192939495969798999a9b9c9d9e9f"
            "a0a1a2a3a4a5a6a7a8a9aaabacadaeaf"
            "b0b1b2b3b4b5b6b7b8b9babbbcbdbebf"
            "c0c1c2c3c4c5c6c7c8c9cacbcccdcecf"
            "d0d1d2d3d4d5d6d7d8d9dadbdcdddedf"
            "e0e1e2e3e4e5e6e7e8e9eaebecedeeeff0"
            "e540"
        ),
        "encoded": (
            "0afb020103ffffff01ff"
            "f30102030405060708090a0b0c0d0e0f"
            "101112131415161718191a1b1c1d1e1f"
            "202122232425262728292a2b2c2d2e2f"
            "303132333435363738393a3b3c3d3e3f"
            "404142434445464748494a4b4c4d4e4f"
            "505152535455565758595a5b5c5d5e5f"
            "606162636465666768696a6b6c6d6e6f"
            "707172737475767778797a7b7c7d7e7f"
            "808182838485868788898a8b8c8d8e8f"
            "909192939495969798999a9b9c9d9e9f"
            "a0a1a2a3a4a5a6a7a8a9aaabacadaeaf"
            "b0b1b2b3b4b5b6b7b8b9babbbcbdbebf"
            "c0c1c2c3c4c5c6c7c8c9cacbcccdcecf"
            "d0d1d2d3d4d5d6d7d8d9dadbdcdddedf"
            "e0e1e2e3e4e5e6e7e8e9eaebecedeeeff0"
            "e54000"
        ),
    },
]


class ProtocolTests(unittest.TestCase):
    def test_crc_known_vectors(self):
        self.assertEqual(p.crc16(b""), 0xFFFF)
        self.assertEqual(p.crc16(b"123456789"), 0x29B1)

    def test_c_vectors_match_python_build_and_encode(self):
        for vector in VECTORS:
            with self.subTest(vector=vector["name"]):
                payload = bytes.fromhex(vector["payload"])
                frame = p.build_frame(
                    vector["src"],
                    vector["dst"],
                    vector["flags"],
                    vector["session"],
                    vector["seq"],
                    vector["family"],
                    vector["type"],
                    payload,
                )
                self.assertEqual(frame.hex(), vector["frame"])
                self.assertEqual(p.cobs_encode(frame).hex(), vector["encoded"])

    def test_c_vectors_parse_and_decode(self):
        for vector in VECTORS:
            with self.subTest(vector=vector["name"]):
                frame = p.parse_frame(bytes.fromhex(vector["frame"]))
                self.assertEqual(frame.src, vector["src"])
                self.assertEqual(frame.dst, vector["dst"])
                self.assertEqual(frame.flags, vector["flags"])
                self.assertEqual(frame.session, vector["session"])
                self.assertEqual(frame.seq, vector["seq"])
                self.assertEqual(frame.family, vector["family"])
                self.assertEqual(frame.type, vector["type"])
                self.assertEqual(frame.payload, bytes.fromhex(vector["payload"]))

                decoded = p.decode_frame(bytes.fromhex(vector["encoded"]))
                self.assertEqual(decoded, frame)

    def test_rejects_bad_length_and_bad_crc(self):
        frame = bytearray(p.build_frame(1, 2, 0, 0, 0, p.FAMILY_NETMGMT, p.NETMGMT_HEARTBEAT))

        bad_length = bytearray(frame)
        bad_length[0] = 99
        with self.assertRaises(p.ArcProtocolError):
            p.parse_frame(bad_length)

        bad_crc = bytearray(frame)
        bad_crc[3] ^= 0x10
        with self.assertRaises(p.ArcProtocolError):
            p.parse_frame(bad_crc)

    def test_ack_matches_c_semantics(self):
        original = p.parse_frame(
            p.build_frame(
                p.ADDR_GROUND,
                p.ADDR_FC_L,
                p.FLAG_RELIABLE,
                5,
                0xABCD,
                p.FAMILY_FC_COORD,
                0x20,
            )
        )
        ack = p.parse_frame(p.build_ack(original, my_session=1, my_seq=9999))
        self.assertEqual(ack.src, p.ADDR_FC_L)
        self.assertEqual(ack.dst, p.ADDR_GROUND)
        self.assertEqual(ack.flags & p.FLAG_ACK, p.FLAG_ACK)
        self.assertEqual(ack.family, p.FAMILY_NETMGMT)
        self.assertEqual(ack.type, p.NETMGMT_ACK)
        self.assertEqual(ack.payload, bytes.fromhex("abcd"))

    def test_size_limits(self):
        p.build_frame(1, 2, 0, 0, 0, 0, 0, bytes(p.MAX_PAYLOAD_SIZE))
        with self.assertRaises(p.ArcBufferError):
            p.build_frame(1, 2, 0, 0, 0, 0, 0, bytes(p.MAX_PAYLOAD_SIZE + 1))


if __name__ == "__main__":
    unittest.main()

