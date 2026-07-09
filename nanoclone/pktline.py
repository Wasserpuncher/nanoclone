"""Git's pkt-line framing format.

Every message in the Git wire protocol is wrapped in a pkt-line: a 4-byte
lowercase hex length prefix (which *includes* the 4 prefix bytes) followed by
the payload. Three lengths are special control packets:

    0000  flush-pkt         end of a section / message
    0001  delim-pkt         separator between capabilities and arguments (v2)
    0002  response-end-pkt  end of a response (v2, stateless-connect)

Reference: https://git-scm.com/docs/protocol-common#_pkt_line_format
"""

from __future__ import annotations

from typing import BinaryIO

DATA = 0
FLUSH = 1
DELIM = 2
RESPONSE_END = 3

FLUSH_PKT = b"0000"
DELIM_PKT = b"0001"
RESPONSE_END_PKT = b"0002"

# A pkt-line is at most 65520 bytes on the wire, 4 of which are the length.
MAX_PAYLOAD = 65516


class ProtocolError(RuntimeError):
    """The peer sent something that is not valid Git wire protocol."""


def encode(payload: bytes) -> bytes:
    """Frame ``payload`` as a single data pkt-line."""
    if len(payload) > MAX_PAYLOAD:
        raise ProtocolError(f"payload of {len(payload)} bytes exceeds pkt-line limit")
    return b"%04x" % (len(payload) + 4) + payload


def encode_text(line: str) -> bytes:
    """Frame ``line`` as a data pkt-line, appending the conventional newline."""
    return encode(line.encode("utf-8") + b"\n")


def read_exactly(stream: BinaryIO, n: int) -> bytes:
    """Read exactly ``n`` bytes or raise. ``BinaryIO.read`` may return short."""
    chunks = []
    remaining = n
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            raise ProtocolError(f"unexpected end of stream, wanted {n} bytes")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_pkt(stream: BinaryIO) -> tuple[int, bytes]:
    """Read one pkt-line.

    Returns a ``(kind, payload)`` pair where kind is one of DATA, FLUSH, DELIM
    or RESPONSE_END. The payload is empty for the control packets. Note that an
    empty *data* packet (``0004``) is legal and distinct from a flush-pkt,
    which is why the kind is returned separately rather than using ``b""`` as a
    sentinel.
    """
    header = read_exactly(stream, 4)
    try:
        length = int(header, 16)
    except ValueError:
        raise ProtocolError(f"malformed pkt-line length {header!r}") from None

    if length == 0:
        return FLUSH, b""
    if length == 1:
        return DELIM, b""
    if length == 2:
        return RESPONSE_END, b""
    if length == 3:
        raise ProtocolError("pkt-line length 0003 is reserved and invalid")
    if length < 4:
        raise ProtocolError(f"pkt-line length {length} is invalid")

    return DATA, read_exactly(stream, length - 4)
