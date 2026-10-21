#!/usr/bin/env python3
"""Independent platform diagnostic: absolute opens during symlink replacement."""
import json
import os
from pathlib import Path
import tempfile
import threading
import time


def main():
    result = {"switches": 300, "reads": 0, "errors": 0, "invalid_reads": 0,
              "error_examples": []}
    with tempfile.TemporaryDirectory(prefix="releasecraft-probe-") as temporary:
        root = Path(temporary).resolve()
        for name in ("one", "two"):
            (root / name).mkdir()
            (root / name / "app.txt").write_text(name)
        (root / "current").symlink_to("one")
        ready, done = threading.Event(), threading.Event()

        def read():
            while not done.is_set():
                try:
                    value = (root / "current/app.txt").read_text()
                    result["reads"] += 1
                    result["invalid_reads"] += value not in ("one", "two")
                except OSError as exc:
                    result["errors"] += 1
                    if len(result["error_examples"]) < 5:
                        result["error_examples"].append({"errno": exc.errno, "message": exc.strerror})
                ready.set()

        reader = threading.Thread(target=read, daemon=True)
        reader.start()
        try:
            if not ready.wait(5):
                raise RuntimeError("reader did not start")
            for index in range(result["switches"]):
                (root / "next").symlink_to("two" if index % 2 == 0 else "one")
                os.replace(root / "next", root / "current")
                time.sleep(0.0001)
        finally:
            done.set()
            reader.join(timeout=5)
        if reader.is_alive():
            raise RuntimeError("reader did not stop")
    print(json.dumps(result, sort_keys=True))
    return int(bool(result["errors"] or result["invalid_reads"] or not result["reads"]))


if __name__ == "__main__":
    raise SystemExit(main())
