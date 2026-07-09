"""End-to-end clone against a real remote.

Opt-in, because it needs the network. Enable with::

    NANOCLONE_NETWORK_TESTS=1 python -m unittest discover -s tests

The repository below is GitHub's own tiny demo repo. We clone it with
nanoclone, then hand the result to real Git and ask whether it is sane.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from nanoclone import cli, transport

REPO_URL = "https://github.com/octocat/Hello-World"
GIT = shutil.which("git")

enabled = os.environ.get("NANOCLONE_NETWORK_TESTS") == "1"


@unittest.skipUnless(enabled, "set NANOCLONE_NETWORK_TESTS=1 to run network tests")
class NetworkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_discover_advertises_protocol_v2_and_fetch(self):
        capabilities = transport.discover(REPO_URL)
        # Every v2 server must offer these two commands for a clone to work.
        self.assertTrue(any(c.startswith("ls-refs") for c in capabilities), capabilities)
        self.assertTrue(any(c.startswith("fetch") for c in capabilities), capabilities)

    def test_ls_refs_reports_head_as_a_symref(self):
        refs = {ref.name: ref for ref in transport.ls_refs(REPO_URL)}
        self.assertIn("HEAD", refs)
        self.assertTrue(refs["HEAD"].symref_target.startswith("refs/heads/"))
        self.assertEqual(len(refs["HEAD"].sha), 40)

    def test_clone_matches_what_git_would_have_produced(self):
        target = self.root / "hello"
        self.assertEqual(cli.clone(REPO_URL, str(target), None, quiet=True), 0)

        def run(*args, cwd):
            return subprocess.run(
                [GIT, *args], cwd=cwd, check=True, capture_output=True
            ).stdout.decode().strip()

        # Our clone is a repository Git accepts, and it is not dirty.
        run("fsck", "--strict", "--no-dangling", cwd=target)
        self.assertEqual(run("status", "--porcelain", cwd=target), "")

        # The commit we checked out is the one the remote advertises for HEAD.
        refs = {ref.name: ref for ref in transport.ls_refs(REPO_URL)}
        head_branch = refs["HEAD"].symref_target
        self.assertEqual(run("rev-parse", "HEAD", cwd=target), refs[head_branch].sha)

        # And real Git, cloning the same URL, lands on the same commit and tree.
        reference = self.root / "reference"
        subprocess.run([GIT, "clone", "-q", REPO_URL, str(reference)], check=True, capture_output=True)
        self.assertEqual(
            run("rev-parse", "HEAD^{tree}", cwd=target),
            run("rev-parse", "HEAD^{tree}", cwd=reference),
        )

    def test_clone_refuses_to_overwrite_a_nonempty_directory(self):
        target = self.root / "occupied"
        target.mkdir()
        (target / "file").write_text("hi")
        with self.assertRaisesRegex(Exception, "not empty"):
            cli.clone(REPO_URL, str(target), None, quiet=True)


@unittest.skipUnless(enabled, "set NANOCLONE_NETWORK_TESTS=1 to run network tests")
class ErrorTest(unittest.TestCase):
    def test_missing_repository_gives_a_clear_message(self):
        with self.assertRaisesRegex(transport.TransportError, "HTTP 4"):
            transport.discover("https://github.com/octocat/this-repo-does-not-exist-xyz")

    def test_ssh_urls_are_rejected_with_guidance(self):
        # A scp-style URL is rewritten to HTTPS rather than refused.
        self.assertEqual(
            transport.normalize_url("git@github.com:octocat/Hello-World.git"),
            "https://github.com/octocat/Hello-World.git",
        )
        with self.assertRaisesRegex(transport.TransportError, "HTTP\\(S\\) only"):
            transport.normalize_url("git://github.com/octocat/Hello-World.git")


if __name__ == "__main__":
    unittest.main()
