"""Packfile parsing, including OFS_DELTA and REF_DELTA resolution.

A packfile is a 12-byte header (``PACK``, 4-byte version, 4-byte object count),
followed by that many compressed objects, followed by a 20-byte SHA-1 checksum
over everything that precedes it.

Each object begins with a variable-length header encoding its type and its
*inflated* size, then the zlib-deflated object payload. Two of the seven types
are deltas: they store a patch against a base object, identified either by a
negative byte offset into the same pack (OFS_DELTA) or by the raw SHA-1 of the
base (REF_DELTA).

References:
    https://git-scm.com/docs/pack-format
    https://git-scm.com/docs/gitformat-pack
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from dataclasses import dataclass

OBJ_COMMIT = 1
OBJ_TREE = 2
OBJ_BLOB = 3
OBJ_TAG = 4
# 5 is reserved for future expansion.
OBJ_OFS_DELTA = 6
OBJ_REF_DELTA = 7

TYPE_NAMES = {
    OBJ_COMMIT: "commit",
    OBJ_TREE: "tree",
    OBJ_BLOB: "blob",
    OBJ_TAG: "tag",
}


class PackError(RuntimeError):
    """The packfile is malformed or references an object we do not have."""


@dataclass(frozen=True)
class RawObject:
    """One entry as it physically appears in the pack, before delta resolution."""

    offset: int
    type_num: int
    inflated_size: int
    data: bytes
    # Exactly one of these is set, and only for delta entries.
    base_offset: int | None = None
    base_sha: str | None = None

    @property
    def is_delta(self) -> bool:
        return self.type_num in (OBJ_OFS_DELTA, OBJ_REF_DELTA)


@dataclass(frozen=True)
class GitObject:
    """A fully resolved object: its type, its bytes, and its identity."""

    sha: str
    type_name: str
    data: bytes

    @property
    def size(self) -> int:
        return len(self.data)


def object_sha(type_name: str, data: bytes) -> str:
    """Compute a Git object id: SHA-1 over ``"<type> <size>\\0" + content``."""
    header = f"{type_name} {len(data)}".encode() + b"\x00"
    return hashlib.sha1(header + data).hexdigest()


def _read_varint_size(buf: bytes, pos: int) -> tuple[int, int, int]:
    """Read a packed object header: returns ``(type_num, size, new_pos)``.

    The first byte carries a continuation bit, a 3-bit type, and the low 4 bits
    of the size. Every further byte contributes 7 more size bits, little-endian.
    """
    byte = buf[pos]
    pos += 1
    type_num = (byte >> 4) & 0x07
    size = byte & 0x0F
    shift = 4
    while byte & 0x80:
        byte = buf[pos]
        pos += 1
        size |= (byte & 0x7F) << shift
        shift += 7
    return type_num, size, pos


def _read_ofs_delta_offset(buf: bytes, pos: int) -> tuple[int, int]:
    """Read the negative base offset of an OFS_DELTA entry.

    This is *not* a plain varint: each continuation adds one before shifting,
    which makes the encoding prefix-free and removes redundant representations.
    """
    byte = buf[pos]
    pos += 1
    offset = byte & 0x7F
    while byte & 0x80:
        byte = buf[pos]
        pos += 1
        offset = ((offset + 1) << 7) | (byte & 0x7F)
    return offset, pos


_INFLATE_CHUNK = 16384


def _inflate(buf: memoryview, pos: int, expected_size: int) -> tuple[bytes, int]:
    """Inflate one zlib stream starting at ``pos``; return data and end offset.

    The pack does not record the *compressed* length of an object, so we let
    zlib tell us where the stream ended: once it reports ``eof``, whatever we
    over-fed it is waiting in ``unused_data``.

    We feed fixed-size chunks of a memoryview rather than handing zlib the whole
    remainder of the pack. Slicing ``bytes`` would copy the tail of the pack once
    per object, which turns parsing into a quadratic operation -- on a 14 MB pack
    with ~27k objects that alone cost about a minute.
    """
    decompressor = zlib.decompressobj()
    chunks: list[bytes] = []
    cursor = pos

    while not decompressor.eof:
        if cursor >= len(buf):
            raise PackError(f"truncated zlib stream at offset {pos}")
        end = min(cursor + _INFLATE_CHUNK, len(buf))
        chunks.append(decompressor.decompress(buf[cursor:end]))
        cursor = end

    # Everything after the stream's end was fed in but not consumed.
    cursor -= len(decompressor.unused_data)

    data = b"".join(chunks)
    if len(data) != expected_size:
        raise PackError(
            f"object at offset {pos} inflated to {len(data)} bytes, "
            f"header promised {expected_size}"
        )
    return data, cursor


def apply_delta(base: bytes, delta: bytes) -> bytes:
    """Apply a Git delta to ``base``.

    The delta starts with the expected source and target sizes as 7-bit
    little-endian varints, followed by a stream of instructions:

    * high bit set  -> copy a run from the base; the low 7 bits say which of the
      four offset bytes and three size bytes follow. A size of 0 means 0x10000.
    * high bit clear-> insert the next ``n`` literal bytes (``n`` must be nonzero).
    """
    pos = 0

    def read_varint() -> int:
        nonlocal pos
        result = 0
        shift = 0
        while True:
            byte = delta[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            shift += 7
            if not byte & 0x80:
                return result

    source_size = read_varint()
    target_size = read_varint()
    if source_size != len(base):
        raise PackError(
            f"delta expects a {source_size}-byte base, got {len(base)} bytes"
        )

    out = bytearray()
    while pos < len(delta):
        opcode = delta[pos]
        pos += 1

        if opcode & 0x80:  # copy from base
            copy_offset = 0
            copy_size = 0
            for i in range(4):
                if opcode & (1 << i):
                    copy_offset |= delta[pos] << (i * 8)
                    pos += 1
            for i in range(3):
                if opcode & (1 << (4 + i)):
                    copy_size |= delta[pos] << (i * 8)
                    pos += 1
            if copy_size == 0:
                copy_size = 0x10000
            if copy_offset + copy_size > len(base):
                raise PackError("delta copy instruction runs past the base object")
            out += base[copy_offset : copy_offset + copy_size]
        elif opcode:  # insert `opcode` literal bytes
            out += delta[pos : pos + opcode]
            pos += opcode
        else:
            raise PackError("delta opcode 0x00 is reserved and invalid")

    if len(out) != target_size:
        raise PackError(
            f"delta produced {len(out)} bytes, header promised {target_size}"
        )
    return bytes(out)


def read_raw_objects(data: bytes) -> list[RawObject]:
    """Parse the pack header and every entry, without resolving deltas."""
    if len(data) < 32:
        raise PackError("packfile is too short to be valid")

    # A memoryview lets us slice into the pack without copying it.
    buf = memoryview(data)
    if buf[:4] != b"PACK":
        raise PackError(f"bad pack signature {bytes(buf[:4])!r}, expected b'PACK'")

    version, count = struct.unpack(">II", buf[4:12])
    if version not in (2, 3):
        raise PackError(f"unsupported pack version {version}")

    if hashlib.sha1(buf[:-20]).digest() != bytes(buf[-20:]):
        raise PackError("packfile checksum mismatch: the data is corrupt")

    objects: list[RawObject] = []
    pos = 12
    for _ in range(count):
        offset = pos
        type_num, size, pos = _read_varint_size(buf, pos)

        base_offset = base_sha = None
        if type_num == OBJ_OFS_DELTA:
            distance, pos = _read_ofs_delta_offset(buf, pos)
            base_offset = offset - distance
            if base_offset < 12:
                raise PackError("OFS_DELTA points before the first object")
        elif type_num == OBJ_REF_DELTA:
            base_sha = buf[pos : pos + 20].hex()
            pos += 20
        elif type_num not in TYPE_NAMES:
            raise PackError(f"unknown object type {type_num} at offset {offset}")

        payload, pos = _inflate(buf, pos, size)
        objects.append(RawObject(offset, type_num, size, payload, base_offset, base_sha))

    if pos != len(buf) - 20:
        raise PackError("trailing garbage between last object and pack checksum")
    return objects


def resolve_all(
    raw_objects: list[RawObject],
    external_base: dict[str, GitObject] | None = None,
) -> tuple[dict[str, GitObject], dict[int, GitObject]]:
    """Resolve every delta; return the objects keyed by SHA-1 *and* by offset.

    Deltas form a forest, not a cycle, but a REF_DELTA may name a base that
    appears *later* in the pack, so a single forward scan is not enough. We
    instead run a fixpoint: repeatedly resolve whatever has become resolvable
    until a full pass makes no progress. A pack whose deltas cannot all be
    resolved is a "thin" pack; ``external_base`` supplies objects from an
    existing store to complete it.
    """
    external_base = external_base or {}
    by_offset: dict[int, GitObject] = {}
    by_sha: dict[str, GitObject] = {}
    pending = list(raw_objects)

    while pending:
        still_pending: list[RawObject] = []
        for raw in pending:
            if not raw.is_delta:
                type_name = TYPE_NAMES[raw.type_num]
                obj = GitObject(object_sha(type_name, raw.data), type_name, raw.data)
            else:
                if raw.type_num == OBJ_OFS_DELTA:
                    base = by_offset.get(raw.base_offset)
                else:
                    base = by_sha.get(raw.base_sha) or external_base.get(raw.base_sha)

                if base is None:
                    still_pending.append(raw)
                    continue

                data = apply_delta(base.data, raw.data)
                obj = GitObject(object_sha(base.type_name, data), base.type_name, data)

            by_offset[raw.offset] = obj
            by_sha[obj.sha] = obj

        if len(still_pending) == len(pending):
            missing = {r.base_sha or f"offset {r.base_offset}" for r in still_pending}
            raise PackError(
                f"cannot resolve {len(still_pending)} delta(s); missing bases: "
                + ", ".join(sorted(missing)[:5])
            )
        pending = still_pending

    return by_sha, by_offset


def resolve(
    raw_objects: list[RawObject],
    external_base: dict[str, GitObject] | None = None,
) -> dict[str, GitObject]:
    """Resolve every delta and return the pack's objects keyed by SHA-1."""
    return resolve_all(raw_objects, external_base)[0]


def parse_pack(buf: bytes) -> dict[str, GitObject]:
    """Parse a complete packfile into ``{sha: GitObject}``."""
    return resolve(read_raw_objects(buf))
