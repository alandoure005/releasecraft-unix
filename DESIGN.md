# Design and tradeoffs

## Store layout

```text
store/                       0700, owned by the invoking user
  .lock                      stable inode; never unlink it while using the store
  store.json                 format marker
  history.json               ordered activation/rollback events
  transaction.json           present only during a pending transition
  current -> releases/v2/payload
  previous -> releases/v1/payload
  releases/                  0700
    v1/                      0700 envelope, never reused by the CLI
      manifest.json          0444
      payload/               directories 0555
        index.html           0444; executable files use 0555
```

The source and destination must not overlap. Every payload component has a
restricted name; no symbolic links or special files are accepted. Directory
traversal uses open directory descriptors and `O_NOFOLLOW`, including child file
opens. A file's identity is compared before reading, and its size/mtime/ctime
are compared afterward. These checks catch ordinary concurrent changes; they
are not a filesystem snapshot and cannot prove source-wide consistency. Stage
from a quiescent build directory.

## Publication

1. Acquire a nonblocking exclusive `flock` on the store's stable `.lock` file.
2. Allocate `.stage-*` inside `releases`, ensuring the final rename stays on one
   filesystem.
3. Copy bytes into new files, hash them, record empty directories and executable
   intent, and make the payload and manifest read-only.
4. Re-read the copied payload and compare its entire inventory with the strict
   manifest. Reject duplicate JSON keys, unexpected fields, malformed entries,
   extra/missing files, symlinks, hard-linked payload files and permission changes.
5. Rename the verified staging envelope to the unused version name and sync its
   parent. The envelope remains 0700 because renaming a read-only directory is not
   portable across all target environments.

The lock prevents cooperating writers from publishing the same version. There is
no overwrite or delete command. A crash after the rename may leave a complete
published version even if the caller did not receive success; retrying the same
version fails safely. `verify` determines whether that version is usable. The
audit log covers activation/rollback transitions, not staging or rejected commands.

## Activation and recovery

A transition stores `{id, kind, from, to, prior_previous, at}` in an atomic JSON
journal before changing pointers. Its target is fully verified again. Each pointer
is installed using a temporary relative symlink followed by `os.replace`:

```text
journal persisted -> previous set to old current -> current set to target
                  -> history event persisted -> journal removed
```

Replacing `current` has no unlink/create gap. It changes one directory entry,
not a transaction spanning every file a consumer may open. Already-open file
descriptors continue to refer to their original files.

Atomic replacement of a directory entry does not establish that every host's
pathname lookup is error-free. The development macOS environment produced
intermittent `EINVAL` from absolute and descriptor-relative opens through a
changing symlink, including in a standalone reproducer with no ReleaseCraft
code. Unlocked `readlink` also reproduced the error. The concurrency test takes a
shared lock on the stable `.lock` inode, captures `current`, releases the lock,
then opens the unchanging version path directly. Its reader stays active until
the writer signals completion and must observe both release contents. This gives
multi-file operations an explicit version to pin and prevents lookup during a
writer's transition. `make probe-absolute` preserves the separate host diagnostic
and reports failures without retries. Uncoordinated consumers are outside this
reader guarantee.

`recover` checks that pointers are compatible with either side of the journal,
verifies the target again, completes the two replacements, and writes the event
once. The transition ID makes recovery idempotent if a crash occurred after the
history write. Inconsistent pointers or corrupted journals fail closed. Recovery
is explicit; the read-only status command never fixes state behind the caller's
back. After recovery, rollback can start a new verified transition.

History is rewritten through a temporary file and atomic replacement instead of
appending a potentially torn JSON line. This keeps the format readable after
ordinary process failures, at the cost of O(number of events) work per activation.
History records transition start times, not a proof of the exact commit instant.

## Boundaries

- Files and affected directories are `fsync`ed where the sequence needs them.
  Actual power-loss durability still depends on the filesystem, storage stack,
  and OS. Tests simulate process/I/O failures, not hardware power loss. NFS and
  distributed filesystems are outside the supported model.
- There is a window between pointer replacements. Readers that need a coherent
  snapshot of both pointers must use this CLI's lock; arbitrary shell reads can
  see intermediate state. Cooperating consumers use a shared lock to capture the
  selected version, then release it before reading that version's immutable files.
- `flock` releases when the descriptor/process closes, so there is no PID-file
  stale-lock cleanup. Advisory locks only work when other writers cooperate.
- Private roots and no-follow opens reduce accidental escapes. They do not
  protect against root or a hostile process sharing the same Unix user. The
  owner can change permissions, replace control files, and recompute hashes.
- Normal exceptions and SIGINT clean private staging. SIGKILL cannot run cleanup;
  leftover staging and temporary metadata files may require manual inspection.
- If disk errors happen after `current` changes, the command can return failure
  while the new release is active. Inspect `status` and finish `recover`; never
  equate a nonzero exit code with "nothing changed."

Future extensions would need their own contracts: signed manifests for provenance,
health checks and compensating actions for service operations, a deliberate
retention policy, and durable external storage for an owner-resistant audit trail.
