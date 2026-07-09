"""Delta application and packfile integrity checks, without touching git."""

import hashlib
import struct
import unittest
import zlib

from nanoclone import packfile
from nanoclone.packfile import PackError, apply_delta


def encode_varint(value: int) -> bytes:
    """The plain 7-bit little-endian varint used for delta sizes."""
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def build_delta(source_size: int, target_size: int, instructions: bytes) -> bytes:
    return encode_varint(source_size) + encode_varint(target_size) + instructions


class ApplyDeltaTest(unittest.TestCase):
    def test_insert_only(self):
        delta = build_delta(0, 5, bytes([5]) + b"hello")
        self.assertEqual(apply_delta(b"", delta), b"hello")

    def test_copy_whole_base(self):
        base = b"abcdefgh"
        # opcode 0x90: no offset bytes (offset 0), one size byte.
        delta = build_delta(len(base), 8, bytes([0x90, 8]))
        self.assertEqual(apply_delta(base, delta), base)

    def test_copy_with_offset_and_insert(self):
        base = b"the quick brown fox"
        # Copy "quick" (offset 4, size 5), then insert "!".
        delta = build_delta(len(base), 6, bytes([0x91, 4, 5]) + bytes([1]) + b"!")
        self.assertEqual(apply_delta(base, delta), b"quick!")

    def test_zero_copy_size_means_65536(self):
        base = b"x" * 0x10000
        delta = build_delta(len(base), 0x10000, bytes([0x80]))  # no size bytes -> 0x10000
        self.assertEqual(apply_delta(base, delta), base)

    def test_multibyte_offset_and_size(self):
        base = bytes(range(256)) * 4  # 1024 bytes
        # opcode 0xB3 = copy | offset bytes 0,1 | size bytes 0,1
        # -> offset 0x0100 (0x00, 0x01), size 0x0102 (0x02, 0x01)
        delta = build_delta(len(base), 0x102, bytes([0x80 | 0x03 | 0x30, 0x00, 0x01, 0x02, 0x01]))
        self.assertEqual(apply_delta(base, delta), base[0x100 : 0x100 + 0x102])

    def test_wrong_base_size_is_rejected(self):
        delta = build_delta(99, 1, bytes([1]) + b"x")
        with self.assertRaisesRegex(PackError, "expects a 99-byte base"):
            apply_delta(b"short", delta)

    def test_wrong_target_size_is_rejected(self):
        delta = build_delta(0, 99, bytes([1]) + b"x")
        with self.assertRaisesRegex(PackError, "produced 1 bytes"):
            apply_delta(b"", delta)

    def test_copy_past_end_of_base_is_rejected(self):
        delta = build_delta(3, 10, bytes([0x91, 0, 10]))
        with self.assertRaisesRegex(PackError, "past the base"):
            apply_delta(b"abc", delta)

    def test_reserved_zero_opcode_is_rejected(self):
        delta = build_delta(0, 0, bytes([0x00]))
        with self.assertRaisesRegex(PackError, "reserved"):
            apply_delta(b"", delta)


class ObjectShaTest(unittest.TestCase):
    def test_matches_the_documented_empty_blob(self):
        # The SHA-1 of an empty blob is a well-known constant in Git.
        self.assertEqual(
            packfile.object_sha("blob", b""),
            "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391",
        )

    def test_matches_hello_world_blob(self):
        # `printf 'hello\n' | git hash-object --stdin`
        self.assertEqual(
            packfile.object_sha("blob", b"hello\n"),
            "ce013625030ba8dba906f756967f9e9ca394464a",
        )


def make_pack(objects: list[tuple[int, bytes]]) -> bytes:
    """Build a minimal valid packfile from ``(type_num, payload)`` pairs."""
    body = bytearray(b"PACK" + struct.pack(">II", 2, len(objects)))
    for type_num, payload in objects:
        size = len(payload)
        first = (type_num << 4) | (size & 0x0F)
        size >>= 4
        header = bytearray()
        while size:
            header.append(first | 0x80)
            first = size & 0x7F
            size >>= 7
        header.append(first)
        body += bytes(header) + zlib.compress(payload)
    return bytes(body) + hashlib.sha1(bytes(body)).digest()


class PackStructureTest(unittest.TestCase):
    def test_parses_a_minimal_pack(self):
        pack = make_pack([(packfile.OBJ_BLOB, b"hello\n")])
        objects = packfile.parse_pack(pack)
        self.assertEqual(len(objects), 1)
        blob = objects["ce013625030ba8dba906f756967f9e9ca394464a"]
        self.assertEqual((blob.type_name, blob.data), ("blob", b"hello\n"))

    def test_large_object_header_size_encoding(self):
        payload = b"A" * 5000  # forces a multi-byte size header
        objects = packfile.parse_pack(make_pack([(packfile.OBJ_BLOB, payload)]))
        self.assertEqual(next(iter(objects.values())).data, payload)

    def test_bad_signature_is_rejected(self):
        with self.assertRaisesRegex(PackError, "bad pack signature"):
            packfile.parse_pack(b"NOPE" + b"\x00" * 28)

    def test_corrupt_checksum_is_rejected(self):
        pack = bytearray(make_pack([(packfile.OBJ_BLOB, b"hi")]))
        pack[-1] ^= 0xFF
        with self.assertRaisesRegex(PackError, "checksum mismatch"):
            packfile.parse_pack(bytes(pack))

    def test_truncated_pack_is_rejected(self):
        with self.assertRaisesRegex(PackError, "too short"):
            packfile.parse_pack(b"PACK")

    def test_unresolvable_ref_delta_is_reported(self):
        missing = "0" * 40
        payload = build_delta(0, 1, bytes([1]) + b"x")
        body = bytearray(b"PACK" + struct.pack(">II", 2, 1))
        body += bytes([(packfile.OBJ_REF_DELTA << 4) | len(payload)])
        body += bytes.fromhex(missing) + zlib.compress(payload)
        pack = bytes(body) + hashlib.sha1(bytes(body)).digest()
        with self.assertRaisesRegex(PackError, "cannot resolve 1 delta"):
            packfile.parse_pack(pack)


if __name__ == "__main__":
    unittest.main()
