# Reasoning about a release tool

## Start with invariants

Before writing commands, decide what must remain true: a version name is never
reused, incomplete staging cannot become `current`, activation checks the exact
bytes it will reference, and failures must leave enough evidence to recover.
These invariants produce more useful tests than simply checking successful output.

## Atomic rename has a precise meaning

`rm current; ln -s ... current` creates an observable gap. Creating a temporary
symlink and renaming it over `current` replaces one directory entry atomically.
That does not make a multi-file read atomic. A consumer can open one file before
the switch and another afterward. Pin a specific release path or directory
descriptor when all files must come from the same version. Capture the selected
path under the store's shared lock as shown in the README: host pathname lookup
can still fail during a concurrent replacement, even though the rename is atomic.

Staging beside the final release matters: renames across filesystems cannot provide
the same publication operation. Checking a command's exit status also matters,
but a failure after a state change is possible. Inspect state rather than assuming
an error means the system is unchanged.

## Locks coordinate; permissions constrain

An advisory lock is a protocol among programs. Holding `flock` stops another
ReleaseCraft process from entering, but does not stop `cp` or a text editor.
Unlinking a lock file would create a second inode that another process could lock,
so the file remains in place and the kernel releases the lock when its descriptor
closes. A dead process does not require guessing whether a stored PID is stale.

Directory write permission controls name changes; execute permission permits
traversal. File read/write bits control access to file contents. The payload is
read-only to reduce accidents, while the publication envelope stays private and
owner-writable for portable rename behavior. The owner can still `chmod` files:
"read-only" and "tamper-proof" describe different promises.

## Hashes, manifests, and trust

SHA-256 answers whether bytes match an expected digest. The manifest also needs a
complete file inventory; otherwise an added malicious file could pass unnoticed.
Hashes alone cannot identify a publisher. Someone able to change both the payload
and manifest can produce a consistent replacement. Authenticity needs a trusted
signature or a separately protected expected digest.

## Make failure tests tell a story

Test what the next operator sees after an interrupted command. This suite injects
failures before pointer changes, between them, before audit persistence and after
it. It also sends a real interrupt during staging, holds the real lock from another
process, and repeatedly captures and reads the selected version during switches. Each test asserts
the resulting state and recovery behavior, rather than only an error message.

## Why Bash and Python work together here

Bash is useful for invoking commands and connecting their exit status to Make or
CI. Quote paths, use `--` where supported, avoid `eval`, and use `exec` when the
wrapper should hand control and signals to the actual program. Python offers
structured JSON, safe argument handling, descriptor-relative filesystem calls,
and testable recovery logic. Choosing the tool that makes an invariant clear is
more valuable than forcing every operation into a single language.

To explore safely: run `make demo`, read one test, predict the state after its
injected failure, then compare that prediction with the assertions and journal
protocol. Use only disposable local stores for experiments.
