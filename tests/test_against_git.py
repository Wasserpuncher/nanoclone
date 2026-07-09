"""Differential tests: nanoclone must agree with real Git, byte for byte.

These tests build a throwaway repository with the ``git`` binary, ask Git to
repack it (which is what produces deltas), and then check that our own parser
recovers exactly the objects Git says are in there. Because every object id is
a SHA-1 over its contents, agreeing on the ids means agreeing on every byte.

Finally we check out the tree ourselves and let Git audit the result: a clean
``git status`` and a passing ``git fsck`` mean our object store, our refs and
our hand-written index are all internally consistent.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nanoclone import packfile, repo

GIT = shutil.which("git")


def git(*args: str, cwd: Path, **kwargs) -> subprocess.CompletedProcess:
    """Run git in a hermetic environment, ignoring the user's own config."""
    env = {
        **os.environ,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "nanoclone tests",
        "GIT_AUTHOR_EMAIL": "tests@example.invalid",
        "GIT_COMMITTER_NAME": "nanoclone tests",
        "GIT_COMMITTER_EMAIL": "tests@example.invalid",
        "GIT_AUTHOR_DATE": "2005-04-07T22:13:13 +0200",
        "GIT_COMMITTER_DATE": "2005-04-07T22:13:13 +0200",
    }
    return subprocess.run(
        [GIT, *args], cwd=cwd, env=env, check=True, capture_output=True, **kwargs
    )


def git_objects(repo_path: Path) -> dict[str, tuple[str, bytes]]:
    """Every object Git knows about, as ``{sha: (type, content)}``.

    ``--batch-all-objects --batch`` streams ``<sha> <type> <size>\\n<content>\\n``.
    """
    out = git("cat-file", "--batch-all-objects", "--batch", cwd=repo_path).stdout
    objects: dict[str, tuple[str, bytes]] = {}
    pos = 0
    while pos < len(out):
        newline = out.index(b"\n", pos)
        sha, type_name, size = out[pos:newline].decode().split(" ")
        start = newline + 1
        end = start + int(size)
        objects[sha] = (type_name, out[start:end])
        pos = end + 1  # skip the trailing newline Git adds after the content
    return objects


def build_fixture_repo(path: Path) -> None:
    """A repo with enough variety to exercise every code path we care about."""
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", "-b", "main", cwd=path)

    # A large, slowly-mutating file is what makes Git choose delta encoding.
    body = "\n".join(f"line {i}: the quick brown fox jumps over the lazy dog" for i in range(4000))
    for revision in range(6):
        (path / "big.txt").write_text(body.replace("fox", f"fox-{revision}"))
        (path / "notes.md").write_text(f"# Revision {revision}\n\nSome prose.\n")
        (path / "nested").mkdir(exist_ok=True)
        (path / "nested" / "deep.txt").write_text(f"depth {revision}\n")
        git("add", "-A", cwd=path)
        git("commit", "-q", "-m", f"revision {revision}", cwd=path)

    # Empty file, executable file, symlink, and a file with non-ASCII bytes.
    (path / "empty").write_bytes(b"")
    (path / "script.sh").write_text("#!/bin/sh\necho hi\n")
    os.chmod(path / "script.sh", 0o755)
    (path / "link").symlink_to("notes.md")
    (path / "umlaut.txt").write_text("Grüße, Straße, Öl\n", encoding="utf-8")
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "modes and encodings", cwd=path)


def single_pack(repo_path: Path) -> Path:
    packs = list((repo_path / ".git" / "objects" / "pack").glob("*.pack"))
    assert len(packs) == 1, f"expected exactly one pack, found {packs}"
    return packs[0]


@unittest.skipIf(GIT is None, "the git binary is required for differential tests")
class AgainstGitTest(unittest.TestCase):
    fixture: Path
    temporary: tempfile.TemporaryDirectory

    @classmethod
    def setUpClass(cls) -> None:
        cls.temporary = tempfile.TemporaryDirectory()
        cls.fixture = Path(cls.temporary.name) / "fixture"
        build_fixture_repo(cls.fixture)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temporary.cleanup()

    def repack(self, *config: str) -> tuple[Path, list[packfile.RawObject]]:
        """Repack the fixture into one pack and parse it with our own code."""
        work = Path(self.temporary.name) / f"repack-{abs(hash(config))}"
        if work.exists():
            shutil.rmtree(work)
        shutil.copytree(self.fixture, work, symlinks=True)

        options = [arg for setting in config for arg in ("-c", setting)]
        git(*options, "repack", "-adf", "--window=250", "--depth=50", cwd=work)
        git("prune-packed", cwd=work)

        raw = packfile.read_raw_objects(single_pack(work).read_bytes())
        return work, raw

    def assert_matches_git(self, work: Path, raw: list[packfile.RawObject]) -> None:
        ours = packfile.parse_pack(single_pack(work).read_bytes())
        theirs = git_objects(work)

        self.assertEqual(set(ours), set(theirs), "object id sets differ")
        for sha, (type_name, content) in theirs.items():
            self.assertEqual(ours[sha].type_name, type_name, f"type differs for {sha}")
            self.assertEqual(ours[sha].data, content, f"content differs for {sha}")

    def test_offset_deltas_match_git(self):
        work, raw = self.repack("repack.usedeltabaseoffset=true")
        kinds = {entry.type_num for entry in raw}
        self.assertIn(packfile.OBJ_OFS_DELTA, kinds, "fixture failed to produce OFS_DELTA")
        self.assert_matches_git(work, raw)

    def test_reference_deltas_match_git(self):
        work, raw = self.repack("repack.usedeltabaseoffset=false")
        kinds = {entry.type_num for entry in raw}
        self.assertIn(packfile.OBJ_REF_DELTA, kinds, "fixture failed to produce REF_DELTA")
        self.assert_matches_git(work, raw)

    def test_verify_pack_agrees_on_every_object(self):
        """Our per-offset resolution must match ``git verify-pack -v`` exactly.

        A wrinkle worth knowing: in verify-pack's ``size`` column Git reports the
        *inflated object* size for a normal entry, but the size of the *delta
        payload* for a delta entry (the resolved object is usually far larger).
        Both numbers appear in our RawObject/GitObject pair, so we check both.
        """
        work, raw = self.repack("repack.usedeltabaseoffset=true")
        _, by_offset = packfile.resolve_all(raw)
        raw_by_offset = {entry.offset: entry for entry in raw}

        out = git("verify-pack", "-v", str(single_pack(work)), cwd=work).stdout.decode()
        expected: dict[int, tuple[str, int, bool]] = {}
        for line in out.splitlines():
            fields = line.split()
            # <sha> <type> <size> <packed-size> <offset> [<depth> <base-sha>]
            if len(fields) >= 5 and len(fields[0]) == 40 and fields[1] in ("commit", "tree", "blob", "tag"):
                is_delta = len(fields) >= 7
                expected[int(fields[4])] = (fields[0], int(fields[2]), is_delta)

        self.assertEqual(len(expected), len(raw), "unexpected verify-pack output")
        self.assertTrue(any(delta for _, _, delta in expected.values()), "no deltas to check")

        for offset, (sha, size, is_delta) in expected.items():
            entry, obj = raw_by_offset[offset], by_offset[offset]
            self.assertEqual(obj.sha, sha, f"sha differs at offset {offset}")
            self.assertEqual(entry.is_delta, is_delta, f"delta-ness differs at offset {offset}")
            if is_delta:
                self.assertEqual(entry.inflated_size, size, f"delta size differs at offset {offset}")
            else:
                self.assertEqual(obj.size, size, f"object size differs at offset {offset}")


@unittest.skipIf(GIT is None, "the git binary is required for differential tests")
class CheckoutTest(unittest.TestCase):
    """Check out a tree ourselves, then let Git audit what we produced."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fixture = self.root / "fixture"
        build_fixture_repo(self.fixture)
        git("repack", "-adf", cwd=self.fixture)
        git("prune-packed", cwd=self.fixture)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_checkout_produces_a_repository_git_calls_clean(self):
        objects = packfile.parse_pack(single_pack(self.fixture).read_bytes())
        head = git("rev-parse", "HEAD", cwd=self.fixture).stdout.decode().strip()

        target = self.root / "clone"
        target.mkdir()
        git_dir = repo.init_repo(target, "https://example.invalid/repo.git", "main")
        for obj in objects.values():
            repo.write_loose_object(git_dir, obj)
        repo.write_ref(git_dir, "refs/heads/main", head)

        tree = repo.commit_tree(objects[head].data)
        entries = repo.checkout(objects, tree, target)
        repo.write_index(git_dir, target, entries)

        # 1. Git agrees on what HEAD is.
        self.assertEqual(git("rev-parse", "HEAD", cwd=target).stdout.decode().strip(), head)

        # 2. The object store is internally consistent and fully connected.
        git("fsck", "--strict", "--no-dangling", cwd=target)

        # 3. Index, HEAD tree and the working tree all agree: nothing to report.
        status = git("status", "--porcelain", cwd=target).stdout.decode()
        self.assertEqual(status, "", f"git reports a dirty tree:\n{status}")

        # 4. Spot-check the special file modes we deliberately created.
        self.assertTrue((target / "link").is_symlink())
        self.assertEqual(os.readlink(target / "link"), "notes.md")
        self.assertTrue(os.access(target / "script.sh", os.X_OK))
        self.assertFalse(os.access(target / "notes.md", os.X_OK))
        self.assertEqual((target / "empty").read_bytes(), b"")
        self.assertEqual((target / "umlaut.txt").read_text(encoding="utf-8"), "Grüße, Straße, Öl\n")

        # 5. The file contents equal what Git itself would have checked out.
        for name in ("big.txt", "notes.md", "nested/deep.txt"):
            self.assertEqual(
                (target / name).read_bytes(),
                (self.fixture / name).read_bytes(),
                f"contents differ for {name}",
            )


if __name__ == "__main__":
    unittest.main()
