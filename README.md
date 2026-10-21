# ReleaseCraft

[![Checks](https://github.com/alandoure005/releasecraft-unix/actions/workflows/check.yml/badge.svg)](https://github.com/alandoure005/releasecraft-unix/actions/workflows/check.yml)

A local release manager for Unix: stage a version, verify its SHA-256 manifest,
atomically switch `current`, and roll back to the previous release. Bash provides
the command entrypoint, Python's standard library handles filesystem operations,
and Make runs checks and demonstrations.

This is a portfolio implementation of release-management mechanics. It works with
local files; it does not launch services, deploy to a cloud, or require `sudo`.

## Quick start

Requirements: Linux or macOS, Bash, Python **3.11+**, and Make for checks. No packages
to install. From this checkout:

```bash
make check
make demo
```

The demo uses and removes only its own temporary directory. To keep a store:

```bash
# Choose an existing parent you own. pwd -P avoids macOS /tmp or /var aliases.
parent="$(pwd -P)"
store="$parent/my-release-store"
./bin/releasecraft --root "$store" init
./bin/releasecraft --root "$store" stage --source examples/site-v1 --version v1
./bin/releasecraft --root "$store" verify --version v1
./bin/releasecraft --root "$store" activate --version v1
cat "$store/current/index.html"

./bin/releasecraft --root "$store" stage --source examples/site-v2 --version v2
./bin/releasecraft --root "$store" activate --version v2
./bin/releasecraft --root "$store" rollback
./bin/releasecraft --root "$store" status
./bin/releasecraft --root "$store" history
```

`init` requires a new store path and an existing parent directory. Every command
requires `--root`; there is no global default or implicit deployment destination.
If choosing a parent through a symlink, first enter the intended directory and use
`pwd -P`. All path components supplied to the tool must be free of symlinks and `..`.

## Commands and results

| Command | Behavior |
| --- | --- |
| `init` | Create a private store and empty activation history. |
| `stage --source DIR --version NAME` | Copy source without modifying it; reject duplicate versions; verify and publish the release. |
| `verify --version NAME` | Check file hashes, sizes, executable flags, permissions, directory inventory and unexpected files. |
| `activate --version NAME` | Re-verify target, save the old release as `previous`, atomically replace `current`. |
| `rollback` | Re-verify and activate `previous`; the former current becomes the new previous. |
| `status` | Report pointers, a pending activation, and abandoned staging-directory names. |
| `history` | Print the activation/rollback audit records, including UTC time and transition ID. |
| `recover` | Re-verify and finish a pending activation forward, without duplicating its audit event. |

Success writes JSON to stdout. Errors go to stderr. Exit codes: **0** success,
**2** invalid request or usage, **3** store busy, **4** verification, state or
filesystem error, **130** interrupted by Ctrl-C. Read commands also acquire the
store lock and return 3 if another command holds it.

Versions contain 1–64 ASCII letters/digits/dots/underscores/hyphens, beginning with
a letter or digit. Payload path components allow those characters but exclude
`.` and `..`. Spaces, backslashes, symlinks, devices, sockets and FIFOs are rejected.
Empty directories and executable files are preserved; ownership, timestamps,
ACLs, extended attributes and original permission bits are not copied.

## Failure handling and scope

Normal failed staging and Ctrl-C clean up only the new private staging directory.
SIGKILL or power loss can leave `.stage-*` remnants; `status` reports them. There
is no automatic retention or deletion of published releases. Inspect abandoned
staging directories manually with no active writer before removing them.

If activation fails, first run `status`. If `pending` is true, run `recover`.
Other mutations are blocked until recovery finishes. Recovery only moves forward;
if target verification fails, restore its exact original contents/permissions from
a trusted copy before retrying, or inspect the store manually. Do not erase the
journal to force an unsafe transition.

Published versions are immutable **through this CLI**, and payloads/manifests are
read-only. The private release envelope remains owner-writable for portable
directory renames. The file owner can change permissions, rewrite a manifest, or
bypass locks; this is not a security boundary against that owner. SHA-256 detects
changes against a manifest, not the identity of whoever produced it.

Use a local filesystem and one trusted Unix account. The lock is advisory and
does not coordinate non-cooperating writers. `current` is replaced atomically;
`previous`, the journal, and history are separate changes. A process opening
multiple files through `current` during a switch can observe different releases;
resolve and pin a version for a multi-file operation. No health checks, database
migrations, distributed coordination, service restarts, or signed artifacts are
implemented. See [DESIGN.md](DESIGN.md) and [LEARNING.md](LEARNING.md).

### Reading during a switch

The development macOS environment intermittently returned `EINVAL` when opening
a file through a rapidly replaced symlink, using both absolute and descriptor-
relative paths. A minimal example without ReleaseCraft reproduced it. This is an
observed environment limitation, not a claim about every macOS system. Even an
unlocked `readlink` can fail here. Cooperating readers take a shared lock while
capturing the pointer, release the lock, then open that unchanged version's files.
The concurrent reader test follows this protocol. Keep the same
captured version for a multi-file operation; arbitrary unlocked reads through
`current` are not guaranteed to be error-free.

For example, a Python reader can use the existing store's stable lock file:

```python
import fcntl
import os
from pathlib import Path

store = Path("/absolute/path/to/my-release-store")
with (store / ".lock").open("rb") as guard:
    fcntl.flock(guard, fcntl.LOCK_SH)
    release_path = os.readlink(store / "current")
print((store / release_path / "index.html").read_text())
```

Published version directories remain available because the CLI never deletes
them. Use the captured path for every file belonging to the same operation.

`make probe-absolute` runs an independent diagnostic with ordinary absolute-path
reads and 300 symlink replacements. It reports reads and filesystem errors as
JSON and exits nonzero if errors occur. It is separate from `make check` because
it measures the host's pathname behavior; the test suite checks cooperating
readers that capture a version under the shared lock. Filesystem errors fail the
checks. The writer test retries only the documented busy exit code, with a deadline.

## Verification

The integration suite uses isolated temporary directories, actual command
processes, an active reader during switches, a real lock, a SIGINT test, and
injected I/O failures at activation boundaries. CI is configured for Linux and
macOS with Python 3.11. A workflow configuration is not a claim that remote CI has
already passed.

License: [MIT](LICENSE).
