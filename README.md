# nanoclone

**`git clone`, written from scratch. No dependencies — not even `git`.**

nanoclone speaks Git's wire protocol directly: it negotiates over Smart HTTP v2,
downloads a packfile, resolves the delta chains inside it, verifies every object
against its SHA-1, and writes a working tree plus a valid `.git` index.

The result is a repository that **real Git considers clean**:

<!-- readme-check: skip=clones-13-MB-over-the-network -->
```console
$ python -m nanoclone clone https://github.com/psf/requests
Cloning into 'requests'...
  branch main -> ...
  received pack: ~13232.5 KiB in ~2.8s
  resolved ~25624 objects
  checked out 130 files

$ cd requests && git status --porcelain   # no output: Git sees nothing to do
$ git fsck --strict                       # every object connects and hashes out
```

One honest footnote about that last command: on `psf/requests` it prints
`badTimezone: invalid author/committer line` for commit `5e6ecdad`. That is not
this program's doing — a real `git clone` of the same repository produces the
identical complaint, because one commit in that history was written with a
malformed timezone years ago. Every object nanoclone wrote is intact and
correctly hashed; the defect it faithfully reproduced was already there.

Nothing here shells out to `git`. The imports are `hashlib`, `zlib`, `struct`,
`urllib`, `os`, `pathlib`, `argparse`, `sys`, `time`, plus `dataclasses` and
`typing` — all standard library, nothing installed.

---

## Why this exists

Git is usually a black box. Cloning a repository looks like one command, but
underneath it is four distinct formats stacked on top of each other, and every
one of them is small enough to read in an afternoon:

| Layer | What it is |
| --- | --- |
| **pkt-line** | A 4-byte hex length prefix in front of every message. Three lengths are reserved as control packets. |
| **Protocol v2** | A request/response RPC over HTTP. `ls-refs` asks what exists, `fetch` asks for objects. |
| **Packfile** | Every object, zlib-deflated. Most are stored as *deltas* against another object. |
| **Object model** | Commits point at trees, trees point at blobs. Each object's name is the SHA-1 of its contents. |

That last property is what makes this project verifiable rather than merely
plausible: if a single bit of our parsing is wrong, the hash changes, and the
object id we compute stops matching the one Git computes. Correctness is not a
matter of opinion here.

## Install

Requires Python 3.10 or newer. There is nothing to install:

<!-- readme-check: skip=clones-this-repo-into-itself -->
```console
$ git clone https://github.com/Wasserpuncher/nanoclone && cd nanoclone
$ python -m nanoclone --help
```

## Usage

```console
# Clone the remote's default branch
$ python -m nanoclone clone https://github.com/octocat/Hello-World

# Pick a branch and a destination
$ python -m nanoclone clone https://github.com/octocat/Hello-World hi -b test

# List the refs a remote advertises, without downloading anything
$ python -m nanoclone ls-remote https://github.com/octocat/Hello-World
7fd1a60b01f91b314f59955a4e4d4e80d8edf11d	HEAD	-> refs/heads/master
7fd1a60b01f91b314f59955a4e4d4e80d8edf11d	refs/heads/master

# Take apart a packfile: types, sizes, offsets, and which object each delta patches
$ python -m nanoclone verify-pack .git/objects/pack/pack-*.pack
```

## How a clone actually works

1. **Discover.** `GET /info/refs?service=git-upload-pack` with the header
   `Git-Protocol: version=2`. The server answers with its capabilities.
2. **`ls-refs`.** A `POST` asking which refs exist. The reply tells us the
   commit id of each branch, and — via `symref-target` — which branch `HEAD`
   points at.
3. **`fetch`.** A `POST` saying `want <commit>` and `done`. Sending `done`
   immediately skips negotiation, so the server replies with one complete
   packfile. Its bytes arrive multiplexed on sideband channel 1, with progress
   on channel 2 and errors on channel 3.
4. **Unpack.** The packfile is `PACK`, a version, an object count, then that
   many deflated objects, then a SHA-1 checksum over everything before it.
5. **Resolve deltas.** Two of the seven object types are not objects at all but
   patches: `OFS_DELTA` names its base by a backwards offset, `REF_DELTA` by the
   base's SHA-1. A delta is a tiny program of *copy this range from the base* and
   *insert these literal bytes*. Because a `REF_DELTA` may point at a base stored
   later in the pack, resolution runs as a fixpoint rather than a single pass.
6. **Check out.** Walk the commit's tree, write blobs with the right modes
   (including symlinks and the executable bit), and write a `.git/index` that
   caches each file's `stat` data — which is what lets `git status` return
   instantly, and what makes it report a *clean* tree instead of a modified one.

## How I know it's correct

Claiming "it works" is cheap. The test suite (`python -m unittest discover -s tests`)
makes Git itself the judge — 39 tests, no mocks of the format:

- **Differential tests against `git`.** A fixture repository is built with the
  real `git` binary and repacked twice — once forcing `OFS_DELTA`, once forcing
  `REF_DELTA`. nanoclone parses the resulting packfiles, and every object id,
  type and byte of content is compared against `git cat-file --batch-all-objects`.
  Agreeing with Git on all 42 SHA-1 hashes means agreeing on every byte.
- **`verify-pack` cross-check.** Our per-offset resolution is compared to
  `git verify-pack -v`, entry by entry, including which entries are deltas.
- **Git audits our checkout.** We write a repository by hand, then ask Git:
  `git fsck --strict` must pass, `git status --porcelain` must be empty, and
  `git rev-parse HEAD` must agree. Symlinks, the executable bit, empty files and
  UTF-8 filenames are checked explicitly.
- **A real clone.** Opt-in (`NANOCLONE_NETWORK_TESTS=1`) tests clone a public
  repository over the network and assert the resulting tree hash equals the one
  produced by `git clone` of the same URL.
- **Adversarial input.** Corrupt checksums, truncated streams, reserved pkt-line
  length `0003`, the reserved delta opcode `0x00`, copy instructions that run
  past the end of their base, and unresolvable deltas are all rejected with a
  specific error rather than a wrong answer.

## Performance

Measured on the 13.8 MB packfile that `git clone` fetches for `psf/requests`
(26 818 objects, 17 560 of them deltas), Python 3.14, warm page cache. That is
more objects than the 25 624 in the transcript above, and deliberately so: this
row measures the *full* clone — every ref — while `nanoclone clone` asks for one
branch. Both numbers drift upward as the repository grows.

| Phase | Time |
| --- | --- |
| Inflate + parse every object | 0.28 s |
| Resolve all 17 560 deltas | 1.16 s |
| Write 26 818 loose objects | 7.27 s |

A full `clone` of that repository, network included, takes about 14 seconds.

An earlier version spent **63 seconds** in the parse phase. The cause was one
line: handing zlib `buf[pos:]` copies the entire remainder of the packfile, once
per object, which is quadratic. Feeding fixed-size chunks of a `memoryview`
instead made that phase 225× faster. It is documented in `packfile.py` so the
next reader doesn't reintroduce it.

## Limitations

These are deliberate — the goal was to understand the protocol, not to replace Git:

- **HTTP(S) only.** No SSH, no `git://`. `scp`-style URLs are rewritten to HTTPS.
- **Protocol v2 required.** Every modern host (GitHub, GitLab, Gitea, Codeberg)
  speaks it; a v0-only server is rejected with a clear message.
- **Clone only.** No push, no incremental fetch, no negotiation with `have`
  lines, no shallow or partial clones.
- **SHA-1 repositories only.** Experimental SHA-256 repositories are not supported.
- **Objects are written loose,** not as a packfile with an index. This is simpler
  to read, but it costs disk: the `psf/requests` clone occupies 124 MB in
  `.git`, where real Git needs 15 MB. It also means no `.idx` file is produced.
- **No `.gitattributes` processing:** no CRLF conversion, no clean/smudge
  filters, no Git LFS. Submodule commits are recorded in the index, but their
  contents are not fetched.

## Auf Deutsch

`nanoclone` implementiert `git clone` von Grund auf neu — ausschließlich mit der
Python-Standardbibliothek, ohne jede Abhängigkeit und ohne das `git`-Binary
aufzurufen. Umgesetzt sind das pkt-line-Format, der Smart-HTTP-Transport
(Protokoll v2), der Packfile-Parser samt Auflösung von OFS- und REF-Deltas sowie
ein Checkout, der einen gültigen Git-Index schreibt.

Der Korrektheitsbeweis liegt in den Tests: Ein Repository wird mit echtem `git`
gebaut und neu gepackt, nanoclone liest es, und **jede einzelne Objekt-ID, jeder
Typ und jedes Byte** wird gegen `git cat-file` verglichen. Anschließend prüft
`git` selbst unser Ergebnis — `git fsck --strict` muss bestehen und
`git status` muss ein sauberes Arbeitsverzeichnis melden.

## References

The implementation follows the official format specifications:

- [pkt-line format](https://git-scm.com/docs/protocol-common#_pkt_line_format)
- [Protocol v2](https://git-scm.com/docs/protocol-v2)
- [Smart HTTP transport](https://git-scm.com/docs/http-protocol)
- [Packfile format](https://git-scm.com/docs/gitformat-pack)
- [Index format](https://git-scm.com/docs/index-format)

## License

MIT — see [LICENSE](LICENSE).
