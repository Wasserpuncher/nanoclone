"""pkt-line framing: encoding, control packets, and malformed input."""

import io
import unittest

from nanoclone import pktline
from nanoclone.pktline import DATA, DELIM, FLUSH, RESPONSE_END, ProtocolError


class EncodeTest(unittest.TestCase):
    def test_length_prefix_includes_itself(self):
        # "0006" = 4 prefix bytes + 2 payload bytes.
        self.assertEqual(pktline.encode(b"hi"), b"0006hi")

    def test_encode_text_appends_newline(self):
        self.assertEqual(pktline.encode_text("version 2"), b"000eversion 2\n")

    def test_empty_payload_is_a_valid_data_packet(self):
        self.assertEqual(pktline.encode(b""), b"0004")

    def test_oversized_payload_is_rejected(self):
        with self.assertRaises(ProtocolError):
            pktline.encode(b"x" * (pktline.MAX_PAYLOAD + 1))


class ReadTest(unittest.TestCase):
    def read(self, raw):
        return pktline.read_pkt(io.BytesIO(raw))

    def test_reads_data(self):
        self.assertEqual(self.read(b"0009hello"), (DATA, b"hello"))

    def test_control_packets(self):
        self.assertEqual(self.read(b"0000"), (FLUSH, b""))
        self.assertEqual(self.read(b"0001"), (DELIM, b""))
        self.assertEqual(self.read(b"0002"), (RESPONSE_END, b""))

    def test_empty_data_packet_is_distinct_from_flush(self):
        self.assertEqual(self.read(b"0004"), (DATA, b""))

    def test_reserved_length_three_is_rejected(self):
        with self.assertRaises(ProtocolError):
            self.read(b"0003")

    def test_non_hex_length_is_rejected(self):
        with self.assertRaises(ProtocolError):
            self.read(b"zzzz")

    def test_truncated_payload_is_rejected(self):
        with self.assertRaises(ProtocolError):
            self.read(b"0009hi")

    def test_roundtrip(self):
        stream = io.BytesIO(pktline.encode(b"a") + pktline.FLUSH_PKT + pktline.encode(b"bb"))
        self.assertEqual(pktline.read_pkt(stream), (DATA, b"a"))
        self.assertEqual(pktline.read_pkt(stream), (FLUSH, b""))
        self.assertEqual(pktline.read_pkt(stream), (DATA, b"bb"))


class ReadExactlyTest(unittest.TestCase):
    def test_reassembles_short_reads(self):
        class Dribble(io.RawIOBase):
            def __init__(self, data):
                self.data = data

            def read(self, n=-1):
                chunk, self.data = self.data[:1], self.data[1:]
                return chunk

        self.assertEqual(pktline.read_exactly(Dribble(b"abcd"), 4), b"abcd")


if __name__ == "__main__":
    unittest.main()
