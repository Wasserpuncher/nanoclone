"""Git's Smart HTTP transport, protocol version 2.

Protocol v2 turns the wire into a simple request/response RPC. The client first
GETs ``/info/refs?service=git-upload-pack`` to learn the server's capabilities,
then POSTs a command to ``/git-upload-pack``. We implement the two commands a
clone needs: ``ls-refs`` and ``fetch``.

References:
    https://git-scm.com/docs/protocol-v2
    https://git-scm.com/docs/http-protocol
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import BinaryIO, Iterator

from . import pktline
from .pktline import DATA, DELIM, FLUSH, RESPONSE_END, ProtocolError

USER_AGENT = "nanoclone/1.0 (+https://github.com/Wasserpuncher/nanoclone)"

# Sideband channels used by the packfile section of a v2 fetch response.
BAND_PACK = 1
BAND_PROGRESS = 2
BAND_ERROR = 3


@dataclass(frozen=True)
class Ref:
    """One advertised reference."""

    name: str
    sha: str
    symref_target: str | None = None
    peeled: str | None = None


class TransportError(RuntimeError):
    """The remote refused the request or is not usable."""


def normalize_url(url: str) -> str:
    """Accept the forms a user is likely to type and return an HTTP base URL."""
    url = url.strip().rstrip("/")
    if url.startswith("git@") and ":" in url:  # git@github.com:owner/repo.git
        host, _, path = url[4:].partition(":")
        url = f"https://{host}/{path}"
    if url.startswith("github.com/"):
        url = "https://" + url
    if not url.startswith(("http://", "https://")):
        raise TransportError(
            f"unsupported URL {url!r}: nanoclone speaks HTTP(S) only, not SSH or git://"
        )
    return url


def _request(url: str, *, data: bytes | None = None, content_type: str | None = None) -> BinaryIO:
    headers = {
        "User-Agent": USER_AGENT,
        # Opt in to protocol v2. A server that does not understand this header
        # simply answers in v0, which we detect and reject below.
        "Git-Protocol": "version=2",
    }
    if content_type:
        headers["Content-Type"] = content_type
        headers["Accept"] = "application/x-git-upload-pack-result"

    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        return urllib.request.urlopen(request, timeout=60)
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read()[:200].decode("utf-8", "replace").strip()
        finally:
            exc.close()
        raise TransportError(
            f"HTTP {exc.code} from {url}"
            + (f": {detail}" if detail else "")
            + ("\nIs the repository private, or the URL misspelled?" if exc.code in (401, 404) else "")
        ) from None
    except urllib.error.URLError as exc:
        raise TransportError(f"cannot reach {url}: {exc.reason}") from None


def discover(url: str) -> set[str]:
    """GET /info/refs and return the server's protocol v2 capabilities."""
    with _request(f"{url}/info/refs?service=git-upload-pack") as response:
        capabilities: set[str] = set()
        saw_version = False

        while True:
            kind, payload = pktline.read_pkt(response)
            if kind == FLUSH:
                # v0 servers terminate the advertisement with a flush; a v2
                # server sends one only after its capability list.
                if saw_version:
                    break
                continue
            if kind != DATA:
                continue

            line = payload.decode("utf-8", "replace").strip()
            if line.startswith("# service="):
                continue  # legacy service announcement, sent by some servers
            if line == "version 2":
                saw_version = True
                continue
            if not saw_version:
                raise TransportError(
                    f"{url} does not support Git protocol v2 (got {line!r}). "
                    "nanoclone requires v2."
                )
            capabilities.add(line)

    if not saw_version:
        raise TransportError(f"{url} did not advertise protocol v2")
    return capabilities


def _command(url: str, command: str, arguments: list[str]) -> BinaryIO:
    body = bytearray()
    body += pktline.encode_text(f"command={command}")
    body += pktline.encode_text(f"agent={USER_AGENT}")
    body += pktline.DELIM_PKT
    for argument in arguments:
        body += pktline.encode_text(argument)
    body += pktline.FLUSH_PKT

    return _request(
        f"{url}/git-upload-pack",
        data=bytes(body),
        content_type="application/x-git-upload-pack-request",
    )


def ls_refs(url: str) -> list[Ref]:
    """Ask the server which refs it has.

    Each response line is ``<oid> <ref-name>`` plus optional space-separated
    attributes, of which we care about ``symref-target:`` (to learn what HEAD
    points at) and ``peeled:`` (the commit an annotated tag resolves to).
    """
    arguments = ["peel", "symrefs", "ref-prefix HEAD", "ref-prefix refs/heads/", "ref-prefix refs/tags/"]
    refs: list[Ref] = []

    with _command(url, "ls-refs", arguments) as response:
        while True:
            kind, payload = pktline.read_pkt(response)
            if kind in (FLUSH, RESPONSE_END):
                break
            if kind != DATA:
                continue

            fields = payload.decode("utf-8").strip().split(" ")
            if len(fields) < 2:
                raise ProtocolError(f"malformed ls-refs line {payload!r}")

            sha, name = fields[0], fields[1]
            symref_target = peeled = None
            for attribute in fields[2:]:
                if attribute.startswith("symref-target:"):
                    symref_target = attribute.partition(":")[2]
                elif attribute.startswith("peeled:"):
                    peeled = attribute.partition(":")[2]
            refs.append(Ref(name, sha, symref_target, peeled))

    return refs


def _iter_pack_data(response: BinaryIO, progress: bool) -> Iterator[bytes]:
    """Yield the raw pack bytes out of a v2 fetch response.

    The response is a sequence of named sections. We skip everything until the
    ``packfile`` section, whose contents are always sideband-encoded: the first
    byte of each packet selects the channel.
    """
    in_packfile = False

    while True:
        kind, payload = pktline.read_pkt(response)
        if kind in (FLUSH, RESPONSE_END):
            if in_packfile:
                return
            continue  # end of a section we are skipping
        if kind == DELIM:
            continue
        if kind != DATA:
            continue

        if not in_packfile:
            line = payload.rstrip(b"\n")
            if line == b"packfile":
                in_packfile = True
            elif line.startswith(b"ERR "):
                raise TransportError(line[4:].decode("utf-8", "replace"))
            continue

        band, chunk = payload[0], payload[1:]
        if band == BAND_PACK:
            yield chunk
        elif band == BAND_PROGRESS:
            if progress:
                sys.stderr.write(chunk.decode("utf-8", "replace"))
                sys.stderr.flush()
        elif band == BAND_ERROR:
            raise TransportError(chunk.decode("utf-8", "replace").strip())
        else:
            raise ProtocolError(f"unknown sideband channel {band}")


def fetch(url: str, wants: list[str], *, progress: bool = False) -> bytes:
    """Fetch a packfile containing everything reachable from ``wants``."""
    if not wants:
        raise ValueError("fetch requires at least one wanted object id")

    arguments = [] if progress else ["no-progress"]
    arguments += [f"want {sha}" for sha in wants]
    # With no "have" lines and an immediate "done", the server skips
    # negotiation and sends a complete (non-thin) pack in one round trip.
    arguments.append("done")

    with _command(url, "fetch", arguments) as response:
        return b"".join(_iter_pack_data(response, progress))
