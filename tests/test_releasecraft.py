"""Integration and failure-injection tests. Every store lives in a temporary directory."""
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
ENTRY = PROJECT / "bin" / "releasecraft"
SPEC = importlib.util.spec_from_file_location("releasecraft", PROJECT / "lib" / "releasecraft.py")
rc = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rc)


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="releasecraft-test-")
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "store"
        self.source = self.base / "source"
        self.source.mkdir()
        (self.source / "app.txt").write_text("release one\n")
        (self.source / "empty").mkdir()
        (self.source / "run.sh").write_text("#!/bin/sh\necho healthy\n")
        (self.source / "run.sh").chmod(0o755)
        self.cli("init")

    def tearDown(self):
        # Test fixtures include intentionally read-only releases. Restore directory
        # permissions only inside this test's private temporary tree for cleanup.
        for base, dirs, _ in os.walk(self.base):
            os.chmod(base, 0o700)
            for name in dirs:
                path = Path(base) / name
                if not path.is_symlink():
                    path.chmod(0o700)
        self.tmp.cleanup()

    def cli(self, *args, code=0, root=None):
        result = subprocess.run([str(ENTRY), "--root", str(root or self.root), *args],
                                text=True, capture_output=True, timeout=10)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return json.loads(result.stdout) if code == 0 else result.stderr

    def stage(self, v="v1"):
        return self.cli("stage", "--source", str(self.source), "--version", v)

    def activate(self, v="v1"):
        return self.cli("activate", "--version", v)

    def write_manifest(self, change):
        p = self.root / "releases/v1/manifest.json"
        p.chmod(0o644)
        data = json.loads(p.read_text())
        change(data)
        p.write_text(json.dumps(data))
        p.chmod(0o444)

    def test_quickstart_and_source_unchanged(self):
        before = {p.name: (p.read_bytes(), p.stat().st_mode) for p in self.source.iterdir() if p.is_file()}
        self.assertEqual(self.stage(), {"staged": "v1", "files": 2})
        self.assertEqual(self.cli("verify", "--version", "v1")["files"], 2)
        self.activate()
        self.assertEqual((self.root / "current/app.txt").read_text(), "release one\n")
        self.assertTrue((self.root / "current/empty").is_dir())
        self.assertEqual(subprocess.check_output([str(self.root / "current/run.sh")], text=True), "healthy\n")
        after = {p.name: (p.read_bytes(), p.stat().st_mode) for p in self.source.iterdir() if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual((self.root / "current/app.txt").stat().st_mode & 0o777, 0o444)

    def test_manifest_deterministic(self):
        self.stage()
        data = json.loads((self.root / "releases/v1/manifest.json").read_text())
        self.assertEqual([e["path"] for e in data["files"]], ["app.txt", "run.sh"])
        self.assertEqual(data["directories"], ["empty"])
        import hashlib
        self.assertEqual(data["files"][0]["sha256"], hashlib.sha256(b"release one\n").hexdigest())

    def test_rollback_swaps_current_and_previous(self):
        self.stage()
        self.activate()
        (self.source / "app.txt").write_text("release two\n")
        self.stage("v2")
        self.activate("v2")
        state = self.cli("rollback")
        self.assertEqual((state["current"], state["previous"]), ("v1", "v2"))
        self.assertEqual((self.root / "current/app.txt").read_text(), "release one\n")
        self.assertEqual([e["kind"] for e in self.cli("history")], ["activate", "activate", "rollback"])
        self.cli("rollback")
        self.assertEqual(self.cli("status")["current"], "v2")

    def test_duplicate_version_keeps_existing_release(self):
        self.stage()
        (self.source / "app.txt").write_text("different")
        self.cli("stage", "--source", str(self.source), "--version", "v1", code=2)
        self.assertEqual((self.root / "releases/v1/payload/app.txt").read_text(), "release one\n")

    def test_tampered_file_refused_before_activation(self):
        self.stage()
        path = self.root / "releases/v1/payload/app.txt"
        path.chmod(0o644)
        path.write_text("tampered")
        path.chmod(0o444)
        self.cli("verify", "--version", "v1", code=4)
        self.cli("activate", "--version", "v1", code=4)
        self.assertIsNone(self.cli("status")["current"])
        self.assertEqual(self.cli("history"), [])

    def test_tampered_previous_refuses_rollback(self):
        self.stage()
        self.activate()
        self.stage("v2")
        self.activate("v2")
        path = self.root / "releases/v1/payload/app.txt"
        path.chmod(0o644)
        self.cli("rollback", code=4)
        self.assertEqual(self.cli("status")["current"], "v2")

    def test_unexpected_and_missing_files_refused(self):
        self.stage()
        payload = self.root / "releases/v1/payload"
        payload.chmod(0o755)
        (payload / "extra").write_text("extra")
        (payload / "extra").chmod(0o444)
        payload.chmod(0o555)
        self.cli("verify", "--version", "v1", code=4)
        payload.chmod(0o755)
        (payload / "extra").unlink()
        (payload / "app.txt").unlink()
        payload.chmod(0o555)
        self.cli("verify", "--version", "v1", code=4)

    def test_manifest_strict_schema_and_types(self):
        self.stage()
        self.write_manifest(lambda d: d["files"][0].update(size=True))
        self.cli("verify", "--version", "v1", code=4)

    def test_manifest_unknown_fields_refused(self):
        self.stage()
        self.write_manifest(lambda d: d.update(unexpected=1))
        self.cli("verify", "--version", "v1", code=4)

    def test_manifest_traversal_refused(self):
        self.stage()
        self.write_manifest(lambda d: d["files"][0].update(path="../../outside"))
        self.cli("verify", "--version", "v1", code=4)

    def test_release_hardlink_refused(self):
        self.stage()
        path = self.root / "releases/v1/payload/app.txt"
        os.link(path, self.base / "external-link")
        self.cli("verify", "--version", "v1", code=4)

    def test_manifest_duplicate_json_keys_refused(self):
        self.stage()
        path = self.root / "releases/v1/manifest.json"
        path.chmod(0o644)
        path.write_text('{"format":1,"format":1}')
        path.chmod(0o444)
        self.cli("verify", "--version", "v1", code=4)

    def test_source_file_and_directory_symlinks_refused(self):
        for name, target in (("link", self.source / "app.txt"), ("dir-link", self.source / "empty")):
            with self.subTest(name=name):
                link = self.source / name
                link.symlink_to(target)
                self.cli("stage", "--source", str(self.source), "--version", "v1", code=4)
                self.assertEqual(list((self.root / "releases").iterdir()), [])
                link.unlink()

    def test_source_root_symlink_refused(self):
        link = self.base / "alias"
        link.symlink_to(self.source)
        self.cli("stage", "--source", str(link), "--version", "v1", code=2)

    def test_special_file_refused_without_blocking(self):
        os.mkfifo(self.source / "pipe")
        self.cli("stage", "--source", str(self.source), "--version", "v1", code=4)
        self.assertEqual(list((self.root / "releases").iterdir()), [])

    def test_versions_reject_traversal_and_absolute_paths(self):
        for v in ("../outside", "/tmp/escape", "..", ".", "a/b", "a\\b", "v 1", "a" * 65):
            with self.subTest(v=v):
                self.cli("stage", "--source", str(self.source), "--version", v, code=2)
        self.assertFalse((self.base / "outside").exists())

    def test_root_traversal_and_symlink_ancestors_refused(self):
        link = self.base / "alias"
        link.symlink_to(self.base)
        self.cli("init", root=link / "other", code=2)
        self.cli("status", root=self.base / "not-there/../store", code=2)
        self.assertFalse((self.base / "other").exists())

    def test_source_store_overlap_refused(self):
        for source in (self.base, self.root, self.root / "releases"):
            self.cli("stage", "--source", str(source), "--version", "v1", code=2)

    def test_payload_symlink_refused(self):
        self.stage()
        p = self.root / "releases/v1/payload"
        p.chmod(0o755)
        (p / "app.txt").unlink()
        (p / "app.txt").symlink_to(self.source / "app.txt")
        p.chmod(0o555)
        self.cli("verify", "--version", "v1", code=4)

    def test_pointer_escape_refused(self):
        (self.root / "current").symlink_to(self.source)
        self.cli("status", code=4)
        self.stage()
        self.cli("activate", "--version", "v1", code=4)
        self.assertEqual((self.source / "app.txt").read_text(), "release one\n")

    def test_lock_contends_and_releases_on_process_exit(self):
        with (self.root / ".lock").open("r+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            self.cli("stage", "--source", str(self.source), "--version", "v1", code=3)
            self.cli("status", code=3)
        self.stage()

    def test_control_symlink_and_unsafe_root_permissions_refused(self):
        history = self.root / "history.json"
        history.unlink()
        history.symlink_to(self.source / "app.txt")
        self.cli("history", code=4)
        self.root.chmod(0o777)
        self.cli("status", code=4)

    def test_failed_stage_cleans_private_directory(self):
        store = rc.Store(self.root)
        def broken_copy(source, destination):
            (destination / "partial").write_text("partial")
            raise OSError("simulated disk error")
        with store.locked(), patch.object(rc, "copy_tree", broken_copy):
            with self.assertRaises(OSError):
                store.stage(self.source, "v1")
        self.assertEqual(list(store.releases.iterdir()), [])
        self.assertIsNone(self.cli("status")["current"])

    def test_sigint_stage_cleans_private_directory(self):
        marker = self.base / "ready"
        program = """
import importlib.util, pathlib, sys, time
spec = importlib.util.spec_from_file_location('rc', sys.argv[1])
rc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rc)
def slow(source, destination):
    (destination / 'partial').write_text('partial')
    pathlib.Path(sys.argv[2]).write_text('ready')
    time.sleep(30)
rc.copy_tree = slow
sys.exit(rc.main(['--root',sys.argv[3],'stage','--source',sys.argv[4],'--version','v1']))
"""
        process = subprocess.Popen([sys.executable, "-c", program, str(PROJECT / "lib/releasecraft.py"),
                                    str(marker), str(self.root), str(self.source)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 5
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(marker.exists(), "child did not reach staging")
            process.send_signal(signal.SIGINT)
            out, err = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 130, out + err)
            self.assertEqual(list((self.root / "releases").iterdir()), [])
            self.cli("status")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate()

    def interrupt_activation(self, fail_on, error=OSError("simulated interruption")):
        self.stage()
        self.activate()
        self.stage("v2")
        store = rc.Store(self.root)
        original = store.set_link
        def fail(name, v):
            if name == fail_on:
                raise error
            return original(name, v)
        with store.locked(), patch.object(store, "set_link", fail):
            with self.assertRaises(OSError):
                store.activate("v2")
        return store

    def test_recover_after_previous_pointer_changed(self):
        self.interrupt_activation("current")
        self.assertTrue(self.cli("status")["pending"])
        self.cli("activate", "--version", "v2", code=4)
        self.assertEqual(self.cli("recover")["current"], "v2")
        self.assertFalse(self.cli("status")["pending"])
        self.assertEqual(len(self.cli("history")), 2)

    def test_recover_before_any_pointer_changed(self):
        self.interrupt_activation("previous")
        self.assertEqual(self.cli("status")["current"], "v1")
        self.cli("recover")
        self.assertEqual(self.cli("status")["current"], "v2")

    def test_recover_after_current_changed_before_audit(self):
        self.stage()
        self.activate()
        self.stage("v2")
        store = rc.Store(self.root)
        original = rc.atomic_json
        def fail_history(path, data):
            if path.name == "history.json":
                raise OSError("simulated history write failure")
            return original(path, data)
        with store.locked(), patch.object(rc, "atomic_json", fail_history):
            with self.assertRaises(OSError):
                store.activate("v2")
        self.assertEqual(self.cli("status")["current"], "v2")
        self.assertTrue(self.cli("status")["pending"])
        self.assertEqual(len(self.cli("history")), 1)
        self.cli("recover")
        self.assertEqual(len(self.cli("history")), 2)

    def test_recovery_rechecks_target_hash(self):
        self.interrupt_activation("current")
        path = self.root / "releases/v2/payload/app.txt"
        path.chmod(0o644)
        path.write_text("tamper")
        path.chmod(0o444)
        self.cli("recover", code=4)
        self.assertEqual(self.cli("status")["current"], "v1")

    def test_recover_after_audit_write_does_not_duplicate(self):
        self.stage()
        store = rc.Store(self.root)
        original = Path.unlink
        def fail_journal(path, *args, **kwargs):
            if path.name == "transaction.json":
                raise OSError("simulated crash after audit")
            return original(path, *args, **kwargs)
        with store.locked(), patch.object(Path, "unlink", fail_journal):
            with self.assertRaises(OSError):
                store.activate("v1")
        self.assertEqual(len(self.cli("history")), 1)
        self.cli("recover")
        self.assertEqual(len(self.cli("history")), 1)

    def test_rollback_before_first_activation_and_same_version_refused(self):
        self.cli("rollback", code=2)
        self.stage()
        self.activate()
        self.cli("activate", "--version", "v1", code=2)
        self.cli("rollback", code=2)
        self.cli("recover", code=2)

    def test_malformed_journal_is_not_followed(self):
        (self.root / "transaction.json").write_text('{"to":"../../outside"}')
        self.cli("recover", code=4)

    def test_cooperating_reader_stays_active_through_switches(self):
        self.stage()
        self.activate()
        (self.source / "app.txt").write_text("release two\n")
        self.stage("v2")
        reader = """
import fcntl, json, os, pathlib, sys, time
# Capture the pointer under the same advisory lock used by the writer.
root_fd=os.open(sys.argv[1], os.O_RDONLY | os.O_DIRECTORY)
lock_fd=os.open('.lock', os.O_RDONLY, dir_fd=root_fd)
control=pathlib.Path(sys.argv[2]); until=time.monotonic()+30
counts={'release one\\n': 0, 'release two\\n': 0}
try:
    while not (control/'done').exists():
        assert time.monotonic()<until, 'writer did not finish before safety deadline'
        fcntl.flock(lock_fd, fcntl.LOCK_SH)
        try:
            target=os.readlink('current', dir_fd=root_fd)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        fd=os.open(target+'/app.txt', os.O_RDONLY, dir_fd=root_fd)
        with os.fdopen(fd, 'r') as stream:
            content=stream.read()
        assert content in counts, repr(content)
        counts[content]+=1
        (control/'ready').touch()
        if content == 'release two\\n':
            (control/'saw-two').touch()
    print(json.dumps(counts))
finally:
    os.close(lock_fd)
    os.close(root_fd)
"""
        control = self.base / "reader-control"
        control.mkdir()
        process = subprocess.Popen([sys.executable, "-c", reader, str(self.root), str(control)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            def assert_reader_active():
                if process.poll() is not None:
                    out, err = process.communicate()
                    self.fail("reader stopped before writer finished: " + out + err)
            def wait_for(name):
                deadline = time.monotonic() + 5
                while not (control / name).exists() and time.monotonic() < deadline:
                    assert_reader_active()
                    time.sleep(0.01)
                self.assertTrue((control / name).exists(), "reader did not signal " + name)
            def switch(v):
                # A shared reader may legitimately make the nonblocking writer
                # return busy. Retry only that documented exit code, with a bound.
                deadline = time.monotonic() + 10
                while True:
                    result = subprocess.run(
                        [str(ENTRY), "--root", str(self.root), "activate", "--version", v],
                        capture_output=True, text=True, timeout=10)
                    if result.returncode != 3:
                        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                        self.assertEqual(json.loads(result.stdout)["current"], v)
                        return
                    self.assertLess(time.monotonic(), deadline, "writer remained busy")
                    time.sleep(0.01)
            wait_for("ready")
            for _ in range(3):
                switch("v2")
                wait_for("saw-two")
                switch("v1")
                assert_reader_active()
            (control / "done").touch()
            out, err = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, out + err)
            counts = json.loads(out)
            self.assertGreater(counts["release one\n"], 0)
            self.assertGreater(counts["release two\n"], 0)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()

    def test_cli_requires_explicit_root(self):
        result = subprocess.run([str(ENTRY), "status"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)


if __name__ == "__main__":
    unittest.main()
