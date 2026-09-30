"""End-to-end smoke test: run the real CLI (watch mode) in dry-run against a
temp folder and verify graceful shutdown drains files that arrived just
before Ctrl+C (the flaw-#19 fix).

What it does
  1. Builds a throwaway config (dry-run, 0.5 s stability window) in a temp dir.
  2. Starts `python -m fs_organizer` as a child process (own process group).
  3. Drops three files while it runs -> each must produce a dry-run line.
  4. Drops one more file and immediately sends Ctrl-Break (the programmatic
     equivalent of Ctrl+C), so the file is still inside its debounce grace
     period when shutdown starts.
  5. Asserts the child exits cleanly AND the late file still got handled —
     pre-fix, it was silently dropped (Watcher.stop claimed to drain but
     didn't, and pool workers exited leaving queued tasks behind).

Run:  python scripts/smoke_dryrun.py     (exits 0 on success)
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

FILES = ["early_0.txt", "early_1.txt", "early_2.txt", "late.txt"]


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="fsorg_e2e_"))
    watch = root / "watch"
    out = root / "out"  # must never be created in dry-run
    watch.mkdir()
    config_path = root / "config.json"
    log_path = root / "run.log"
    config_path.write_text(json.dumps({
        "watch_folders": [str(watch)],
        "target_rules": {".txt": "Documents"},
        "target_root": str(out),
        "ignore_patterns": ["*.tmp"],
        "dry_run": True,
        "file_stable_seconds": 0.5,
        "ai": {"enabled": False},
    }), encoding="utf-8")

    # The real CLI (__main__.main) in a child with its own process group so we
    # can Ctrl-Break it. SIGBREAK -> default_int_handler makes the child take
    # the same KeyboardInterrupt shutdown path a real Ctrl+C takes.
    child_code = (
        "import signal, sys, runpy\n"
        "if hasattr(signal, 'SIGBREAK'):\n"
        "    signal.signal(signal.SIGBREAK, signal.default_int_handler)\n"
        f"sys.argv = ['fs-organizer', {str(config_path)!r},"
        f" '--log-file', {str(log_path)!r}, '-v']\n"
        "runpy.run_module('fs_organizer', run_name='__main__')\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", child_code],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
        if not ok:
            failures.append(name)

    def wait_log(pattern: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                if pattern in log_path.read_text(encoding="utf-8", errors="replace"):
                    return True
            except OSError:
                pass
            time.sleep(0.05)
        return False

    print(f"Temp dir: {root}\n")
    started = wait_log("Watching", 15)
    check("watcher started", started)
    if not started:
        proc.kill()
        print((proc.stdout.read() if proc.stdout else "") or "(no child output)")
        print(f"Artifacts kept for inspection: {root}")
        return 1

    # 1) Files created while running -> debounced, then dry-run logged.
    for i in range(3):
        (watch / f"early_{i}.txt").write_text(f"early {i}", encoding="utf-8")
    check("early files handled in dry-run", wait_log("Would move", 10))

    # 2) The drain scenario: create a file and shut down immediately, while it
    #    is still inside file_stable_seconds. Pre-fix (#19) it was dropped.
    (watch / "late.txt").write_text("late", encoding="utf-8")
    if os.name == "nt":
        os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
    else:
        proc.send_signal(signal.SIGINT)
    try:
        rc = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        rc = None
    check("clean exit (KeyboardInterrupt shutdown path)", rc == 0, f"rc={rc}")

    stdout = proc.stdout.read() if proc.stdout else ""
    check("'Stopping...' printed (graceful path taken)", "Stopping..." in stdout)

    log = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    would_moves = [line for line in log.splitlines() if "[dry-run] Would move" in line]
    missing = [n for n in FILES if not any(n in line for line in would_moves)]
    check("all 4 files got dry-run lines (late file drained on shutdown)",
          not missing, f"missing: {missing}" if missing else "4/4")

    check("dry-run created no output dirs", not out.exists())
    check("dry-run moved nothing",
          all((watch / n).exists() for n in FILES))

    print("\nDry-run lines from the child:")
    for line in would_moves:
        print(f"  {line}")

    if failures:
        print(f"\nFAILED ({len(failures)}): {', '.join(failures)}")
        print(f"Artifacts kept for inspection: {root}")
        return 1
    shutil.rmtree(root, ignore_errors=True)
    print("\nOK: shutdown drained the late file; dry-run touched nothing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
