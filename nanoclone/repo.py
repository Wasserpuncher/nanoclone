"""Writing a working repository: object store, refs, checkout, and index.

The goal of this module is not merely to place files on disk, but to leave
behind a directory that real Git considers *clean*: the object store, HEAD, the
refs and the index must all agree with the checked-out tree.
"""

from __future__ import annotations

import hashlib
import os
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

from .packfile import GitObject

# Git's tree entry modes. Trees store them as octal ASCII without a leading
# zero, which is why 40000 (not 040000) appears on the wire.
MODE_TREE = 0o040000
MODE_BLOB = 0o100644
MODE_BLOB_EXEC = 0o100755
MODE_SYMLINK = 0o120000
MODE_GITLINK = 0o160000


class RepoError(RuntimeError):
    """The repository cannot be written, or an expected object is missing."""


@dataclass(frozen=True)
class TreeEntry:
    mode: int
    name: str
    sha: str


def parse_tree(data: bytes) -> list[TreeEntry]:
    """Parse a tree object: repeated ``<mode> <name>\\0<20-byte sha>``."""
    entries: list[TreeEntry] = []
    pos = 0
    while pos < len(data):
        space = data.index(b" ", pos)
        null = data.index(b"\x00", space)
        mode = int(data[pos:space], 8)
        name = data[space + 1 : null].decode("utf-8", "surrogateescape")
        sha = data[null + 1 : null + 21].hex()
        if len(sha) != 40:
            raise RepoError("truncated tree entry")
        entries.append(TreeEntry(mode, name, sha))
        pos = null + 21
    return entries


def commit_tree(data: bytes) -> str:
    """Extract the tree id from a commit object's header."""
    for line in data.split(b"\n"):
        if not line:
            break  # blank line ends the header, the rest is the message
        key, _, value = line.partition(b" ")
        if key == b"tree":
            return value.decode()
    raise RepoError("commit object has no tree header")


def init_repo(path: Path, origin_url: str, branch: str) -> Path:
    """Create the .git directory skeleton and return it."""
    git_dir = path / ".git"
    for sub in ("objects", "refs/heads", "refs/tags", "refs/remotes/origin"):
        (git_dir / sub).mkdir(parents=True, exist_ok=True)

    (git_dir / "HEAD").write_text(f"ref: refs/heads/{branch}\n")
    (git_dir / "config").write_text(
        "[core]\n"
        "\trepositoryformatversion = 0\n"
        "\tfilemode = true\n"
        "\tbare = false\n"
        "\tlogallrefupdates = true\n"
        '[remote "origin"]\n'
        f"\turl = {origin_url}\n"
        "\tfetch = +refs/heads/*:refs/remotes/origin/*\n"
        f'[branch "{branch}"]\n'
        "\tremote = origin\n"
        f"\tmerge = refs/heads/{branch}\n"
    )
    return git_dir


def write_loose_object(git_dir: Path, obj: GitObject) -> None:
    """Store one object as a loose, zlib-compressed file under .git/objects."""
    directory = git_dir / "objects" / obj.sha[:2]
    target = directory / obj.sha[2:]
    if target.exists():
        return
    directory.mkdir(parents=True, exist_ok=True)

    header = f"{obj.type_name} {len(obj.data)}".encode() + b"\x00"
    # Write to a temp file and rename, so a crash cannot leave a torn object.
    temporary = target.with_suffix(".tmp")
    temporary.write_bytes(zlib.compress(header + obj.data))
    temporary.replace(target)


def write_ref(git_dir: Path, ref: str, sha: str) -> None:
    path = git_dir / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{sha}\n")


@dataclass(frozen=True)
class IndexEntry:
    path: str  # forward-slash separated, relative to the work tree
    sha: str
    mode: int


def checkout(
    objects: dict[str, GitObject],
    tree_sha: str,
    work_tree: Path,
    prefix: str = "",
) -> list[IndexEntry]:
    """Materialise ``tree_sha`` into ``work_tree``; return the index entries."""
    tree = objects.get(tree_sha)
    if tree is None or tree.type_name != "tree":
        raise RepoError(f"missing tree object {tree_sha}")

    entries: list[IndexEntry] = []
    for entry in parse_tree(tree.data):
        relative = f"{prefix}{entry.name}"
        target = work_tree / relative

        if entry.mode == MODE_TREE:
            target.mkdir(parents=True, exist_ok=True)
            entries += checkout(objects, entry.sha, work_tree, f"{relative}/")
            continue

        if entry.mode == MODE_GITLINK:
            # A submodule: Git records the commit id but stores no content here.
            target.mkdir(parents=True, exist_ok=True)
            entries.append(IndexEntry(relative, entry.sha, MODE_GITLINK))
            continue

        blob = objects.get(entry.sha)
        if blob is None or blob.type_name != "blob":
            raise RepoError(f"missing blob {entry.sha} for {relative}")

        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.mode == MODE_SYMLINK:
            if target.is_symlink() or target.exists():
                target.unlink()
            target.symlink_to(blob.data.decode("utf-8", "surrogateescape"))
        else:
            target.write_bytes(blob.data)
            os.chmod(target, 0o755 if entry.mode == MODE_BLOB_EXEC else 0o644)

        entries.append(IndexEntry(relative, entry.sha, entry.mode))

    return entries


def write_index(git_dir: Path, work_tree: Path, entries: list[IndexEntry]) -> None:
    """Write a version 2 .git/index so that ``git status`` reports a clean tree.

    Each entry caches the file's stat data. Git compares those cached values
    against the filesystem to decide, cheaply, whether a file may have changed;
    if they do not match reality the file is re-hashed, and if we wrote them
    wrongly Git would report spurious modifications.

    Reference: https://git-scm.com/docs/index-format
    """
    body = bytearray()
    body += b"DIRC" + struct.pack(">II", 2, len(entries))

    # Git requires entries sorted by path bytes; it binary-searches the index.
    for entry in sorted(entries, key=lambda e: e.path.encode()):
        name = entry.path.encode("utf-8", "surrogateescape")

        if entry.mode == MODE_GITLINK:
            # Nothing of the submodule is ours to stat; Git accepts zeroes here.
            ctime = mtime = (0, 0)
            dev = ino = size = uid = gid = 0
        else:
            info = os.lstat(work_tree / entry.path)
            ctime = (int(info.st_ctime), info.st_ctime_ns % 1_000_000_000)
            mtime = (int(info.st_mtime), info.st_mtime_ns % 1_000_000_000)
            uid, gid = info.st_uid, info.st_gid
            # The on-disk index reserves 32 bits for each of these; Git
            # truncates the same way when it compares, so overflow is harmless.
            dev = info.st_dev & 0xFFFFFFFF
            ino = info.st_ino & 0xFFFFFFFF
            size = info.st_size & 0xFFFFFFFF

        # The flags word holds the name length, saturating at 0xFFF.
        flags = min(len(name), 0xFFF)

        entry_bytes = struct.pack(
            ">10I20sH",
            ctime[0], ctime[1], mtime[0], mtime[1],
            dev, ino, entry.mode, uid, gid, size,
            bytes.fromhex(entry.sha), flags,
        ) + name

        # Pad with NULs to a multiple of 8 bytes, always at least one NUL.
        padding = 8 - (len(entry_bytes) % 8)
        body += entry_bytes + b"\x00" * padding

    body += hashlib.sha1(body).digest()
    (git_dir / "index").write_bytes(bytes(body))
