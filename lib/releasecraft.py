#!/usr/bin/env python3
"""A local, single-user release store. Standard library only; POSIX platforms."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import uuid

VERSION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
NAME = re.compile(r"[A-Za-z0-9._-]+\Z")
HASH = re.compile(r"[0-9a-f]{64}\Z")
NOFOLLOW = os.O_NOFOLLOW


class Failure(Exception):
    def __init__(self, message, code=2):
        super().__init__(message)
        self.code = code


def require(condition, message, code=2):
    if not condition:
        raise Failure(message, code)


def version(value, code=2):
    require(isinstance(value, str) and VERSION.fullmatch(value), "invalid version", code)
    return value


def safe_name(value):
    require(isinstance(value, str) and NAME.fullmatch(value)
            and value not in (".", ".."), "unsupported filename")
    return value


def relative(value):
    require(isinstance(value, str) and value and not value.startswith("/"),
            "invalid manifest path", 4)
    for part in value.split("/"):
        require(NAME.fullmatch(part) and part not in (".", ".."), "invalid manifest path", 4)
    return value


def absolute(value):
    """Reject traversal and symlink components, rather than silently resolve them."""
    p = Path(value).expanduser()
    require(".." not in p.parts, "parent traversal is not allowed")
    p = Path(os.path.abspath(p))
    for component in reversed([p, *p.parents]):
        require(not component.is_symlink(), "symlink path component is not allowed")
    return p


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def regular(path):
    info = path.lstat()
    require(stat.S_ISREG(info.st_mode), "expected regular control file", 4)


def read_json(path):
    regular(path)
    fd = os.open(path, os.O_RDONLY | NOFOLLOW)
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, "duplicate JSON key", 4)
            result[key] = value
        return result
    try:
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            return json.load(handle, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as exc:
        raise Failure("invalid JSON control file", 4) from exc


def atomic_json(path, data):
    if path.exists() or path.is_symlink():
        regular(path)
    fd, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, sort_keys=True, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        fsync_dir(path.parent)
    finally:
        if os.path.lexists(name):
            os.unlink(name)


def tree(directory, copy_to=None):
    """Walk opened directories; never follow source or payload symlinks."""
    files, directories = [], []
    root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW)

    def walk(fd, prefix):
        with os.scandir(fd) as scan:
            names = sorted(entry.name for entry in scan)
        for name in names:
            safe_name(name)
            rel = f"{prefix}/{name}" if prefix else name
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | NOFOLLOW, dir_fd=fd)
                try:
                    require(os.fstat(child).st_ino == before.st_ino,
                            "directory changed during scan", 4)
                    directories.append(rel)
                    if copy_to is not None:
                        (copy_to / rel).mkdir(mode=0o700)
                    walk(child, rel)
                finally:
                    os.close(child)
            else:
                require(stat.S_ISREG(before.st_mode),
                        "symlinks and special files are not allowed", 4)
                child = os.open(name, os.O_RDONLY | NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
                out = None
                try:
                    opened = os.fstat(child)
                    require(stat.S_ISREG(opened.st_mode)
                            and (opened.st_dev, opened.st_ino) == (before.st_dev, before.st_ino),
                            "file changed during scan", 4)
                    digest = hashlib.sha256()
                    size = 0
                    if copy_to is not None:
                        out = (copy_to / rel).open("xb")
                    while True:
                        chunk = os.read(child, 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        size += len(chunk)
                        if out is not None:
                            out.write(chunk)
                    after = os.fstat(child)
                    require((opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns)
                            == (after.st_size, after.st_mtime_ns, after.st_ctime_ns),
                            "file changed during scan", 4)
                    executable = bool(opened.st_mode & 0o111)
                    if out is not None:
                        out.flush()
                        os.fchmod(out.fileno(), 0o555 if executable else 0o444)
                        os.fsync(out.fileno())
                    else:
                        require(opened.st_mode & 0o7777 == (0o555 if executable else 0o444),
                                "release file permissions changed", 4)
                        require(opened.st_nlink == 1, "hard-linked release file", 4)
                    files.append({"path": rel, "size": size, "sha256": digest.hexdigest(),
                                  "executable": executable})
                finally:
                    if out is not None:
                        out.close()
                    os.close(child)
        if copy_to is None:
            require(os.fstat(fd).st_mode & 0o7777 == 0o555,
                    "release directory permissions changed", 4)
    try:
        walk(root_fd, "")
    finally:
        os.close(root_fd)
    return sorted(files, key=lambda item: item["path"]), sorted(directories)


def copy_tree(source, destination):
    return tree(source, destination)


def freeze(path):
    for base, dirs, _ in os.walk(path, topdown=False):
        for name in dirs:
            os.chmod(Path(base) / name, 0o555)
        fsync_dir(base)
    os.chmod(path, 0o555)
    fsync_dir(path)


def remove_private_stage(path):
    # Only a newly allocated, uncommitted staging directory reaches this function.
    for base, dirs, _ in os.walk(path):
        os.chmod(base, 0o700)
        for name in dirs:
            os.chmod(Path(base) / name, 0o700)
    shutil.rmtree(path)


class Store:
    def __init__(self, root):
        self.root = absolute(root)
        self.releases = self.root / "releases"

    def init(self):
        require(self.root.parent.is_dir(), "root parent must already exist")
        require(not self.root.exists(), "root already exists")
        self.root.mkdir(mode=0o700)
        try:
            self.releases.mkdir(mode=0o700)
            atomic_json(self.root / "store.json", {"format": 1})
            atomic_json(self.root / "history.json", [])
            fd = os.open(self.root / ".lock", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            fsync_dir(self.root)
        except BaseException:
            shutil.rmtree(self.root)
            raise
        return {"root": str(self.root), "initialized": True}

    @contextmanager
    def locked(self):
        for path in (self.root, self.releases):
            info = path.lstat()
            require(stat.S_ISDIR(info.st_mode), "invalid store directory", 4)
            require(info.st_uid == os.getuid() and not info.st_mode & 0o022,
                    "store must be user-owned and not group/world writable", 4)
        metadata = read_json(self.root / "store.json")
        require(isinstance(metadata, dict) and set(metadata) == {"format"}
                and type(metadata["format"]) is int and metadata["format"] == 1,
                "unsupported store", 4)
        regular(self.root / ".lock")
        fd = os.open(self.root / ".lock", os.O_RDWR | NOFOLLOW)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Failure("store is locked by another process", 3) from exc
            yield
        finally:
            os.close(fd)

    def no_pending(self):
        require(not os.path.lexists(self.root / "transaction.json"),
                "pending activation: run recover", 4)

    def read_link(self, name):
        path = self.root / name
        if not os.path.lexists(path):
            return None
        require(path.is_symlink(), "invalid release pointer", 4)
        target = os.readlink(path)
        parts = target.split("/")
        require(len(parts) == 3 and parts[0] == "releases" and parts[2] == "payload",
                "release pointer escapes store", 4)
        v = version(parts[1], 4)
        require(target == f"releases/{v}/payload", "invalid release pointer", 4)
        for item in (self.releases / v, self.releases / v / "payload"):
            require(stat.S_ISDIR(item.lstat().st_mode), "invalid release pointer target", 4)
        return v

    def history(self):
        data = read_json(self.root / "history.json")
        require(isinstance(data, list), "invalid history", 4)
        seen = set()
        for entry in data:
            validate_transaction(entry)
            require(entry["id"] not in seen, "duplicate history transaction", 4)
            seen.add(entry["id"])
        return data

    def status(self):
        return {"current": self.read_link("current"), "previous": self.read_link("previous"),
                "pending": os.path.lexists(self.root / "transaction.json"),
                "staging_remnants": sorted(p.name for p in self.releases.iterdir()
                                           if p.name.startswith(".stage-"))}

    def stage(self, source, v):
        self.no_pending()
        v = version(v)
        source = absolute(source)
        require(source.is_dir(), "source must be a directory")
        require(source != self.root and self.root not in source.parents
                and source not in self.root.parents, "source and store must not overlap")
        target = self.releases / v
        require(not os.path.lexists(target), "version already exists")
        staging = Path(tempfile.mkdtemp(prefix=".stage-", dir=self.releases))
        try:
            payload = staging / "payload"
            payload.mkdir(mode=0o700)
            files, directories = copy_tree(source, payload)
            atomic_json(staging / "manifest.json",
                        {"format": 1, "version": v, "files": files, "directories": directories})
            os.chmod(staging / "manifest.json", 0o444)
            freeze(payload)
            fsync_dir(staging)
            self.verify_at(staging, v)
            os.rename(staging, target)
            fsync_dir(self.releases)
        finally:
            if staging.exists():
                remove_private_stage(staging)
        return {"staged": v, "files": len(files)}

    def verify_at(self, path, v):
        info = path.lstat()
        # Keep the envelope owner-writable for portable directory renames.
        # The manifest and complete payload remain read-only.
        require(stat.S_ISDIR(info.st_mode) and info.st_mode & 0o7777 == 0o700,
                "invalid release directory", 4)
        require(set(os.listdir(path)) == {"manifest.json", "payload"},
                "unexpected release contents", 4)
        manifest = read_json(path / "manifest.json")
        validate_manifest(manifest, v)
        require((path / "manifest.json").stat().st_mode & 0o7777 == 0o444,
                "manifest permissions changed", 4)
        files, directories = tree(path / "payload")
        require(files == manifest["files"] and directories == manifest["directories"],
                "release content does not match manifest", 4)
        return {"verified": v, "files": len(files)}

    def verify(self, v):
        return self.verify_at(self.releases / version(v), v)

    def set_link(self, name, v):
        path = self.root / name
        self.read_link(name)  # Refuse an existing regular file or escaping pointer.
        if v is None:
            if os.path.lexists(path):
                path.unlink()
                fsync_dir(self.root)
            return
        temporary = self.root / (".link-" + uuid.uuid4().hex)
        try:
            temporary.symlink_to(f"releases/{version(v)}/payload")
            os.replace(temporary, path)
            fsync_dir(self.root)
        finally:
            if os.path.lexists(temporary):
                temporary.unlink()

    def activate(self, v, kind="activate"):
        self.no_pending()
        v = version(v)
        self.verify(v)
        old = self.read_link("current")
        prior_previous = self.read_link("previous")
        require(old != v, "release is already current")
        require(old is not None or prior_previous is None, "previous without current", 4)
        self.history()  # Refuse malformed history before changing anything.
        txn = {"id": uuid.uuid4().hex, "kind": kind, "from": old, "to": v,
               "prior_previous": prior_previous,
               "at": datetime.now(timezone.utc).isoformat()}
        atomic_json(self.root / "transaction.json", txn)
        return self.recover()

    def rollback(self):
        self.no_pending()
        previous = self.read_link("previous")
        require(previous is not None, "no previous release")
        return self.activate(previous, "rollback")

    def recover(self):
        journal = self.root / "transaction.json"
        require(os.path.lexists(journal), "no pending activation")
        txn = read_json(journal)
        validate_transaction(txn)
        self.verify(txn["to"])
        current, previous = self.read_link("current"), self.read_link("previous")
        require(current in (txn["from"], txn["to"])
                and previous in (txn["prior_previous"], txn["from"]),
                "pointers conflict with pending activation", 4)
        events = self.history()
        matching = [event for event in events if event["id"] == txn["id"]]
        require(not matching or matching == [txn], "history conflicts with activation", 4)
        self.set_link("previous", txn["from"])
        self.set_link("current", txn["to"])
        if not matching:
            atomic_json(self.root / "history.json", events + [txn])
        journal.unlink()
        fsync_dir(self.root)
        return {"current": txn["to"], "previous": txn["from"], "action": txn["kind"]}


def validate_manifest(data, v):
    require(isinstance(data, dict) and set(data) ==
            {"format", "version", "files", "directories"}, "invalid manifest fields", 4)
    require(type(data["format"]) is int and data["format"] == 1 and data["version"] == v,
            "invalid manifest identity", 4)
    require(isinstance(data["files"], list) and isinstance(data["directories"], list),
            "invalid manifest entries", 4)
    paths = []
    for entry in data["files"]:
        require(isinstance(entry, dict) and set(entry) == {"path", "size", "sha256", "executable"},
                "invalid manifest file", 4)
        paths.append(relative(entry["path"]))
        require(type(entry["size"]) is int and entry["size"] >= 0
                and isinstance(entry["sha256"], str) and HASH.fullmatch(entry["sha256"])
                and type(entry["executable"]) is bool, "invalid manifest file metadata", 4)
    dirs = [relative(item) for item in data["directories"]]
    require(paths == sorted(set(paths)) and dirs == sorted(set(dirs))
            and not set(paths) & set(dirs), "duplicate or unsorted manifest paths", 4)


def validate_transaction(data):
    require(isinstance(data, dict) and set(data) ==
            {"id", "kind", "from", "to", "prior_previous", "at"}, "invalid transaction", 4)
    require(isinstance(data["id"], str) and re.fullmatch(r"[0-9a-f]{32}", data["id"])
            and data["kind"] in ("activate", "rollback") and isinstance(data["at"], str),
            "invalid transaction metadata", 4)
    try:
        require(datetime.fromisoformat(data["at"]).tzinfo is not None, "invalid timestamp", 4)
    except ValueError as exc:
        raise Failure("invalid timestamp", 4) from exc
    version(data["to"], 4)
    for field in ("from", "prior_previous"):
        if data[field] is not None:
            version(data[field], 4)
    require(data["from"] != data["to"] and
            (data["from"] is not None or data["prior_previous"] is None),
            "invalid transition", 4)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, help="explicit local release store")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "status", "history", "rollback", "recover"):
        commands.add_parser(command)
    stage = commands.add_parser("stage")
    stage.add_argument("--source", required=True)
    stage.add_argument("--version", required=True)
    for command in ("verify", "activate"):
        commands.add_parser(command).add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        store = Store(args.root)
        if args.command == "init":
            result = store.init()
        else:
            with store.locked():
                if args.command == "stage":
                    result = store.stage(args.source, args.version)
                elif args.command in ("verify", "activate"):
                    result = getattr(store, args.command)(args.version)
                else:
                    result = getattr(store, args.command)()
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        return 0
    except Failure as exc:
        print("releasecraft: " + str(exc), file=sys.stderr)
        return exc.code
    except (OSError, UnicodeError) as exc:
        # Avoid outputting source contents or dumping a traceback on normal failures.
        print("releasecraft: filesystem operation failed: " + str(exc), file=sys.stderr)
        return 4
    except KeyboardInterrupt:
        print("releasecraft: interrupted; inspect status before retrying", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
