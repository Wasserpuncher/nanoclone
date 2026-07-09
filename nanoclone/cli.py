"""Command line interface: ``clone``, ``ls-remote`` and ``verify-pack``."""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import packfile, repo, transport
from .packfile import PackError
from .pktline import ProtocolError
from .repo import RepoError
from .transport import TransportError

HEAD = "HEAD"


def _default_directory(url: str) -> str:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    return name[:-4] if name.endswith(".git") else name


def _pick_branch(refs: list[transport.Ref], requested: str | None) -> tuple[str, str]:
    """Return ``(branch_name, commit_sha)`` for the branch we should check out."""
    by_name = {ref.name: ref for ref in refs}

    if requested:
        full = f"refs/heads/{requested}"
        if full not in by_name:
            available = sorted(r.name[11:] for r in refs if r.name.startswith("refs/heads/"))
            raise TransportError(
                f"remote has no branch {requested!r}. Available: {', '.join(available) or '(none)'}"
            )
        return requested, by_name[full].sha

    head = by_name.get(HEAD)
    if head is None:
        raise TransportError("remote did not advertise HEAD; use --branch to pick one")
    if not head.symref_target:
        raise TransportError("remote HEAD is detached; use --branch to pick one")
    if not head.symref_target.startswith("refs/heads/"):
        raise TransportError(f"remote HEAD points at {head.symref_target}, which is not a branch")
    return head.symref_target[len("refs/heads/") :], head.sha


def clone(url: str, directory: str | None, branch: str | None, quiet: bool) -> int:
    url = transport.normalize_url(url)
    target = Path(directory or _default_directory(url)).resolve()

    if target.exists() and any(target.iterdir()):
        raise RepoError(f"destination {target} already exists and is not empty")

    def log(message: str) -> None:
        if not quiet:
            print(message, file=sys.stderr)

    log(f"Cloning into '{target.name}'...")
    transport.discover(url)
    refs = transport.ls_refs(url)
    branch_name, commit_sha = _pick_branch(refs, branch)
    log(f"  branch {branch_name} -> {commit_sha[:12]}")

    started = time.monotonic()
    pack = transport.fetch(url, [commit_sha], progress=not quiet)
    log(f"  received pack: {len(pack) / 1024:.1f} KiB in {time.monotonic() - started:.1f}s")

    objects = packfile.parse_pack(pack)
    log(f"  resolved {len(objects)} objects")

    if commit_sha not in objects:
        raise PackError(f"pack does not contain the requested commit {commit_sha}")

    target.mkdir(parents=True, exist_ok=True)
    git_dir = repo.init_repo(target, url, branch_name)
    for obj in objects.values():
        repo.write_loose_object(git_dir, obj)

    repo.write_ref(git_dir, f"refs/heads/{branch_name}", commit_sha)
    repo.write_ref(git_dir, f"refs/remotes/origin/{branch_name}", commit_sha)
    for ref in refs:
        if ref.name.startswith("refs/tags/") and ref.sha in objects:
            repo.write_ref(git_dir, ref.name, ref.sha)

    tree_sha = repo.commit_tree(objects[commit_sha].data)
    entries = repo.checkout(objects, tree_sha, target)
    repo.write_index(git_dir, target, entries)
    log(f"  checked out {len(entries)} files")

    return 0


def ls_remote(url: str) -> int:
    url = transport.normalize_url(url)
    transport.discover(url)
    for ref in transport.ls_refs(url):
        suffix = f"\t-> {ref.symref_target}" if ref.symref_target else ""
        print(f"{ref.sha}\t{ref.name}{suffix}")
        if ref.peeled:
            print(f"{ref.peeled}\t{ref.name}^{{}}")
    return 0


def verify_pack(path: str) -> int:
    """Parse a .pack file and print one line per object.

    Columns are ``sha type size offset [stored-as base]``. Note that ``size`` is
    the size of the *resolved* object; ``git verify-pack -v`` instead prints the
    delta payload size in that column for delta entries.
    """
    raw = packfile.read_raw_objects(Path(path).read_bytes())
    by_sha, by_offset = packfile.resolve_all(raw)
    deltas = sum(1 for entry in raw if entry.is_delta)

    for entry in raw:
        obj = by_offset[entry.offset]
        stored = ""
        if entry.type_num == packfile.OBJ_OFS_DELTA:
            stored = f"  ofs-delta of {by_offset[entry.base_offset].sha}"
        elif entry.type_num == packfile.OBJ_REF_DELTA:
            stored = f"  ref-delta of {entry.base_sha}"
        print(f"{obj.sha} {obj.type_name:<6} {obj.size:>9} {entry.offset:>9}{stored}")

    print(f"\n{len(raw)} objects, {deltas} deltas, {len(by_sha)} unique", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="nanoclone",
        description="A Git client written from scratch, using only the Python standard library.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    clone_parser = subparsers.add_parser("clone", help="clone a repository over HTTP(S)")
    clone_parser.add_argument("url")
    clone_parser.add_argument("directory", nargs="?")
    clone_parser.add_argument("-b", "--branch", help="branch to check out (default: remote HEAD)")
    clone_parser.add_argument("-q", "--quiet", action="store_true")

    ls_parser = subparsers.add_parser("ls-remote", help="list the refs a remote advertises")
    ls_parser.add_argument("url")

    verify_parser = subparsers.add_parser("verify-pack", help="list the objects inside a .pack file")
    verify_parser.add_argument("packfile")

    args = parser.parse_args(argv)

    try:
        if args.command == "clone":
            return clone(args.url, args.directory, args.branch, args.quiet)
        if args.command == "ls-remote":
            return ls_remote(args.url)
        if args.command == "verify-pack":
            return verify_pack(args.packfile)
    except (TransportError, PackError, RepoError, ProtocolError) as exc:
        print(f"nanoclone: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130

    return 2
