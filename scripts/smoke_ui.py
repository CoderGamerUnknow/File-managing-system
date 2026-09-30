"""End-to-end UI smoke test: run the real CLI with --ui against a temp folder,
drop files, and verify the dashboard's HTTP API reflects real activity.

Run:  python scripts/smoke_ui.py     (exits 0 on success)
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def wait_for(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001 - server may still be starting
            last = exc
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}: last={last!r}")


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="fsorg_ui_"))
    watch = root / "watch"
    out = root / "out"
    watch.mkdir()
    config_path = root / "config.json"
    config_path.write_text(json.dumps({
        "watch_folders": [str(watch)],
        "target_rules": {".txt": "Documents", ".png": "Images"},
        "target_root": str(out),
        "ignore_patterns": ["*.tmp"],
        "file_stable_seconds": 0.3,
        "ai": {"enabled": False},
    }), encoding="utf-8")

    proc = subprocess.Popen(
        [sys.executable, "-m", "fs_organizer", str(config_path), "--ui",
         "--port", "0", "--no-browser", "--log-file", str(root / "run.log")],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        encoding="utf-8", errors="replace",
    )
    failures: list[str] = []
    port = None
    try:
        # The CLI currently logs the fixed/default port (--port 0 is not
        # resolved to the ephemeral port in the log line). Find the real
        # port by scanning the log for "Dashboard running at".
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and port is None:
            try:
                log = (root / "run.log").read_text(encoding="utf-8", errors="replace")
                if "Dashboard running at http://127.0.0.1:" in log:
                    port = int(log.split("127.0.0.1:")[1].split("/")[0])
            except OSError:
                pass
            time.sleep(0.1)
        if port is None:
            proc.kill()
            print("FAIL: dashboard never announced its port")
            print((root / "run.log").read_text(encoding="utf-8", errors="replace"))
            return 1

        base = f"http://127.0.0.1:{port}"
        wait_for(lambda: urllib.request.urlopen(base, timeout=2).status == 200,
                 10, "dashboard to come up")

        # Drop two files while watching; the watcher should organize them.
        (watch / "doc.txt").write_text("hello", encoding="utf-8")
        (watch / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        def events_ok():
            # Wait until BOTH files are organized, not merely until the first
            # 'moved' event shows up — otherwise the file listing below races
            # the second move (this flaked the script before).
            data = json.loads(urllib.request.urlopen(base + "/api/events", timeout=2).read())
            names = {e["name"] for e in data["events"] if e["kind"] == "moved"}
            return {"doc.txt", "pic.png"} <= names
        wait_for(events_ok, 15, "both watcher moves to appear in /api/events")

        status = json.loads(urllib.request.urlopen(base + "/api/status", timeout=2).read())
        # /api/files must now list the two organized files grouped by date.
        files = json.loads(urllib.request.urlopen(base + "/api/files", timeout=2).read())
        all_rows = [f for day_files in files["groups"].values() for f in day_files]
        month_keys = [m["key"] for m in files.get("months", [])]
        checks = [
            ("status lists target root", status["target_root"] == str(out)),
            ("status groups categories",
             set(status["categories"]) == {"Documents", "Images"}),
            ("moved file landed in target",
             (out / "Documents" / "doc.txt").exists()),
            ("png landed in target", (out / "Images" / "pic.png").exists()),
            ("/api/files lists both files", files["total"] == 2),
            ("/api/files rows carry names",
             {f["name"] for f in all_rows} == {"doc.txt", "pic.png"}),
            ("/api/files days sorted newest first",
             list(files["groups"]) == sorted(files["groups"], reverse=True)),
            ("/api/files has per-day summary",
             all("count" in s and "bytes" in s and "categories" in s
                 for s in files.get("summary", {}).values())),
            ("/api/files has month index", sum(m["count"] for m in files.get("months", [])) == 2),
            ("month keys are YYYY-MM", all(len(k) == 7 and k[4] == "-" for k in month_keys)),
        ]

        # Token-protected manual scan endpoint.
        req = urllib.request.Request(
            base + "/api/once", method="POST",
            data=json.dumps({"action": "scan"}).encode("utf-8"),
            headers={"Content-Type": "application/json",
                     "X-Auth-Token": "wrong-token"},
        )
        try:
            urllib.request.urlopen(req, timeout=5)
            checks.append(("bad token rejected", False))
        except urllib.error.HTTPError as exc:
            checks.append(("bad token rejected", exc.code in (401, 403)))

        for name, ok in checks:
            print(f"  {'PASS' if ok else 'FAIL'}  {name}")
            if not ok:
                failures.append(name)

        print("\nAPI status:", json.dumps(status, indent=2)[:400])
        print("API files:", json.dumps(files, indent=2)[:400])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()

    if failures:
        print(f"\nFAILED: {len(failures)} check(s)")
        return 1
    print("\nOK: UI dashboard reflected live organizer activity.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
