"""Tests for the local web dashboard (fs_organizer.ui)."""
import json
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from fs_organizer.ui import (
    ActivityLog,
    Dashboard,
    DashboardState,
    status_payload,
)

from helpers import make_config as _make_config


def make_config(tmp_path):
    """File-local default: this suite's fixtures also map .png -> Images."""
    return _make_config(tmp_path, rules={".txt": "Documents", ".png": "Images"})


def free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestActivityLog:
    def test_add_and_snapshot_newest_first(self):
        log = ActivityLog()
        log.add("moved", "a.txt", "-> out")
        log.add("skipped", "b.tmp")
        events = log.snapshot()
        assert [e["kind"] for e in events] == ["skipped", "moved"]
        assert events[0]["name"] == "b.tmp"

    def test_ring_buffer_cap(self):
        log = ActivityLog(max_events=5)
        for i in range(20):
            log.add("moved", f"f{i}.txt")
        events = log.snapshot()
        assert len(events) == 5
        # Newest kept, oldest dropped.
        assert events[0]["name"] == "f19.txt"

    def test_survives_unusual_paths(self):
        log = ActivityLog()
        log.add("moved", 12345)  # non-path junk must not raise
        assert log.snapshot()[0]["name"] == "12345"


class TestStatusPayload:
    def test_groups_rules_by_category(self, tmp_path):
        cfg = make_config(tmp_path)
        s = status_payload(cfg)
        assert s["categories"] == {
            "Documents": [".txt"],
            "Images": [".png"],
        }
        assert s["watch_folders"] == [str(tmp_path / "watch")]
        assert s["target_root"] == str(tmp_path / "out")
        assert s["dry_run"] is False

    def test_ai_summary(self, tmp_path):
        cfg = make_config(tmp_path)
        cfg.ai.enabled = True
        cfg.ai.provider = "ollama"
        cfg.ai.extensions = [".xyz"]
        s = status_payload(cfg)
        assert s["ai"]["enabled"] is True
        assert s["ai"]["extensions"] == [".xyz"]


def _wait_for_server(url: str, timeout: float = 5.0) -> None:
    """Poll until the test server accepts connections (CI machines are slow)."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            urllib.request.urlopen(url, timeout=2)
            return
        except urllib.error.HTTPError:
            return  # server is up (it answered with an HTTP status)
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            last_exc = exc
            time.sleep(0.05)
    raise AssertionError(f"server never came up at {url}: {last_exc}")


class TestDashboard:
    def test_serves_page_and_api(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            html = urllib.request.urlopen(dash.url, timeout=5).read().decode("utf-8")
            assert "fs-organizer" in html
            assert dash.state.token in html  # token injected into the page

            status = json.loads(urllib.request.urlopen(dash.url + "api/status", timeout=5).read())
            assert status["target_root"] == str(tmp_path / "out")
            assert status["oneshot"]["status"] == "idle"

            events = json.loads(urllib.request.urlopen(dash.url + "api/events", timeout=5).read())
            assert events == {"events": []}
        finally:
            dash.stop()

    def test_post_requires_token(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            req = urllib.request.Request(
                dash.url + "api/once",
                data=json.dumps({"action": "scan"}).encode(),
                headers={"Content-Type": "application/json"},  # no token
                method="POST",
            )
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(req, timeout=5)
            assert exc_info.value.code == 401
        finally:
            dash.stop()

    def test_post_with_token_runs_scan(self, tmp_path):
        cfg = make_config(tmp_path)
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")

        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            req = urllib.request.Request(
                dash.url + "api/once",
                data=json.dumps({"action": "scan"}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-Auth-Token": dash.state.token,
                },
                method="POST",
            )
            resp = json.loads(urllib.request.urlopen(req, timeout=5).read())
            assert resp["ok"] is True

            # The scan runs async; wait for it to finish.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                st = json.loads(urllib.request.urlopen(dash.url + "api/status", timeout=5).read())
                if st["oneshot"]["status"] == "done":
                    break
                time.sleep(0.05)
            assert st["oneshot"]["status"] == "done"
            assert st["oneshot"]["moved"] == 1
            assert not src.exists()
            assert (tmp_path / "out" / "Documents" / "a.txt").exists()
            events = json.loads(urllib.request.urlopen(dash.url + "api/events", timeout=5).read())
            kinds = [e["kind"] for e in events["events"]]
            assert "moved" in kinds
        finally:
            dash.stop()

    def test_no_token_no_404_leaks(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                urllib.request.urlopen(dash.url + "api/nope", timeout=5)
            assert exc_info.value.code == 404
        finally:
            dash.stop()

    def test_stop_is_idempotent(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        dash.stop()
        dash.stop()  # must not raise
        assert dash._server is None

    def test_starts_only_once(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        try:
            first = dash._server
            dash.start()  # second start must be a no-op, not a port clash
            assert dash._server is first
        finally:
            dash.stop()


class TestFilesPayload:
    def test_build_groups_sorts_days_newest_first(self):
        from fs_organizer.ui import _build_groups

        # Pure-function test: portable to every OS/FS (birthtime isn't settable,
        # so creation dates can't be faked directly on real files).
        day1 = 1_790_000_000  # 2026-09-15-ish (local time)
        day2 = day1 + 86_400  # one day later
        rows = [
            {"name": "older.txt", "category": "Docs", "created": day1, "size": 1},
            {"name": "newer.txt", "category": "Docs", "created": day2, "size": 2},
            {"name": "other.txt", "category": "Pics", "created": day1 + 60, "size": 3},
        ]
        groups = _build_groups(rows)
        days = list(groups)
        assert days == sorted(days, reverse=True), "groups must be newest day first"
        assert [f["name"] for f in groups[days[0]]] == ["newer.txt"]
        assert {f["name"] for f in groups[days[1]]} == {"older.txt", "other.txt"}

    def test_summary_per_day_counts_bytes_categories(self):
        from fs_organizer.ui import _build_groups, _build_summary

        day = 1_790_000_000
        rows = [
            {"name": "a.txt", "category": "Documents", "created": day, "size": 100},
            {"name": "b.txt", "category": "Documents", "created": day + 1, "size": 50},
            {"name": "c.png", "category": "Images", "created": day + 2, "size": 25},
            {"name": "loose", "category": "", "created": day + 3, "size": 5},
        ]
        summary = _build_summary(_build_groups(rows))
        (entry,) = summary.values()
        assert entry["count"] == 4
        assert entry["bytes"] == 180
        assert entry["categories"] == {"(root)": 1, "Documents": 2, "Images": 1}

    def test_months_index_newest_first(self):
        from fs_organizer.ui import _build_groups, _build_months

        sep1 = 1_790_000_000  # 2026-09-21 local
        sep2 = sep1 + 86_400  # next day, same month
        aug1 = 1_787_400_000  # 2026-08-22 local
        rows = [
            {"name": "a", "category": "D", "created": sep1, "size": 1},
            {"name": "b", "category": "D", "created": sep2, "size": 1},
            {"name": "c", "category": "D", "created": aug1, "size": 1},
        ]
        months = _build_months(_build_groups(rows))
        assert [m["key"] for m in months] == sorted(
            [m["key"] for m in months], reverse=True
        )
        assert months[0]["count"] == 2 and months[1]["count"] == 1
        assert months[0]["label"].endswith("2026")
        assert len(months[0]["days"]) == 2

    def test_payload_includes_summary_and_months(self, tmp_path):
        from fs_organizer.ui import files_payload

        root = tmp_path / "out" / "Documents"
        root.mkdir(parents=True)
        (root / "a.txt").write_text("x", encoding="utf-8")

        data = files_payload(make_config(tmp_path))
        assert set(data["summary"]) == set(data["groups"])
        day = next(iter(data["summary"]))
        assert data["summary"][day]["count"] == 1
        assert data["summary"][day]["bytes"] == 1
        assert len(data["months"]) == 1
        assert data["months"][0]["count"] == 1
        assert data["months"][0]["days"] == [day]

    def test_files_scanned_and_counted(self, tmp_path):
        from fs_organizer.ui import files_payload

        root = tmp_path / "out"
        docs = root / "Documents"
        docs.mkdir(parents=True)
        (docs / "a.txt").write_text("1234", encoding="utf-8")
        (root / "b.png").write_bytes(b"xx")

        data = files_payload(make_config(tmp_path))
        assert data["total"] == 2
        assert data["truncated"] is False
        assert data["target_root"] == str(root)
        all_rows = [f for files in data["groups"].values() for f in files]
        by_name = {f["name"]: f for f in all_rows}
        assert by_name["a.txt"]["category"] == "Documents"
        assert by_name["a.txt"]["size"] == 4
        assert by_name["b.png"]["category"] == ""  # directly in the root
        assert isinstance(by_name["b.png"]["created"], int)

    def test_empty_root_returns_empty_groups(self, tmp_path):
        from fs_organizer.ui import files_payload

        data = files_payload(make_config(tmp_path))
        assert data["groups"] == {} and data["total"] == 0
        assert data["target_root"] == str(tmp_path / "out")

    def test_missing_root_returns_empty_groups(self, tmp_path):
        from fs_organizer.ui import files_payload

        cfg = make_config(tmp_path)
        cfg.target_root = str(tmp_path / "never-created")
        data = files_payload(cfg)
        assert data["groups"] == {} and data["total"] == 0

    def test_max_files_cap_sets_truncated(self, tmp_path):
        from fs_organizer.ui import files_payload

        root = tmp_path / "out"
        root.mkdir(parents=True)
        for i in range(5):
            (root / f"f{i}.txt").write_text("x", encoding="utf-8")
        data = files_payload(make_config(tmp_path), max_files=3)
        assert data["total"] == 3
        assert data["truncated"] is True

    def test_files_endpoint_served(self, tmp_path):
        cfg = make_config(tmp_path)
        root = tmp_path / "out" / "Documents"
        root.mkdir(parents=True)
        (root / "a.txt").write_text("x", encoding="utf-8")

        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            data = json.loads(
                urllib.request.urlopen(dash.url + "api/files", timeout=5).read()
            )
            assert data["total"] == 1
            day = list(data["groups"])[0]
            assert data["groups"][day][0]["name"] == "a.txt"
            # The served page must reference the new card and endpoint.
            html = urllib.request.urlopen(dash.url, timeout=5).read().decode("utf-8")
            assert "Files by date" in html
            assert "/api/files" in html
            # Month navigation + size formatting are wired into the page.
            assert "month-select" in html
            assert "month-prev" in html and "month-next" in html
            assert "fmtSize" in html
        finally:
            dash.stop()


class TestActivityLogDisplayName:
    def test_display_name_overrides_path_name(self):
        log = ActivityLog()
        log.add("moved", "C:/watch/a.txt", "-> out", display_name="a (1).txt")
        assert log.snapshot()[0]["name"] == "a (1).txt"

    def test_display_name_survives_number_paths(self):
        log = ActivityLog()
        log.add("moved", 12345, "", display_name="x.bin")
        assert log.snapshot()[0]["name"] == "x.bin"


class TestDashboardErrorPaths:
    def test_start_failure_raises_and_state_consistent(self, tmp_path, monkeypatch):
        """A bind failure becomes a clean RuntimeError; state stays retryable.

        (Port-in-use can't be simulated portably — Windows SO_REUSEADDR lets a
        second listener bind the same port — so patch the constructor.)
        """
        import fs_organizer.ui as ui_mod

        def boom(*a, **k):
            raise OSError(48, "Address already in use")

        monkeypatch.setattr(ui_mod, "ThreadingHTTPServer", boom)
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        with pytest.raises(RuntimeError, match="Cannot bind dashboard"):
            dash.start()
        assert dash._server is None  # retryable, not stuck started

    def test_stop_after_failed_start_is_safe(self, tmp_path):
        import socket

        cfg = make_config(tmp_path)
        # Occupy a port with a plain socket so the dashboard cannot bind it.
        blocker = socket.socket()
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        port = blocker.getsockname()[1]
        dash = Dashboard(cfg, port=port, open_browser=False)
        try:
            with pytest.raises(RuntimeError):
                dash.start()
            dash.stop()  # must not raise even though start failed
            assert dash._server is None
        finally:
            blocker.close()

    def test_oversized_post_body_rejected(self, tmp_path):
        cfg = make_config(tmp_path)
        dash = Dashboard(cfg, port=free_port(), open_browser=False)
        dash.start()
        _wait_for_server(dash.url)
        try:
            req = urllib.request.Request(
                dash.url + "api/once",
                data=b"x" * (65 * 1024 + 1),
                headers={"Content-Type": "application/json",
                         "X-Auth-Token": dash.state.token},
                method="POST",
            )
            try:
                urllib.request.urlopen(req, timeout=5)
            except urllib.error.HTTPError as exc:
                assert exc.code == 400
            except OSError:
                # The handler answers 400 and closes while the client is still
                # uploading; Windows then aborts the client socket (WinError
                # 10053) before the response is delivered. A mid-upload abort
                # IS the rejection — treat it as a pass.
                pass
            else:
                pytest.fail("server accepted an oversized body")
        finally:
            dash.stop()


class TestWatcherActivityFeed:
    def test_organizer_reports_to_activity(self, tmp_path):
        from fs_organizer.ui import ActivityLog
        from fs_organizer.watcher import Organizer

        cfg = make_config(tmp_path)
        activity = ActivityLog()
        src = tmp_path / "watch" / "a.txt"
        src.write_text("x", encoding="utf-8")

        Organizer(cfg, activity=activity).handle(src)
        kinds = [e["kind"] for e in activity.snapshot()]
        assert "moved" in kinds

    def test_watcher_accepts_activity_kwarg(self, tmp_path):
        from fs_organizer.watcher import Watcher

        cfg = make_config(tmp_path)
        activity = ActivityLog()
        watcher = Watcher(cfg, activity=activity)
        assert watcher.organizer.activity is activity
        watcher.start()
        watcher.stop()  # full start/stop cycle must work with the feed attached
