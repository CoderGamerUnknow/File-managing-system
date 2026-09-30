"""Runtime daemon helpers: the single-instance guard.

Two organizers watching the same folders would race every event: both
debounce the same file, both call move_file, and only the per-directory
locks stand between them — meanwhile the activity feed shows double
events and the journal records both. The guard makes the second process
exit cleanly (exit code 3) instead.

Design (low-resource-daemon skill): one stat() at startup, one atomic
create at acquire, nothing at runtime. A lock left behind by a hard
crash (power loss, kill -9) is detected as stale and replaced — the
guard must never require manual cleanup after an unclean shutdown.
"""
from __future__ import annotations

import os
from pathlib import Path


class InstanceLock:
    """Exclusive pid-lock file guarding one config's daemon.

    The lock file lives next to nothing important — callers choose the
    path (the dashboard/CLI derive it from the config path so two
    different configs can run side by side, but one config cannot).
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._held = False

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Is a process with this pid running right now?"""
        if pid <= 0:
            return False
        if os.name == "nt":
            # No kill(pid, 0) on Windows; probe via OpenProcess.
            import ctypes

            SYNCHRONIZE = 0x00100000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True  # exists, owned by someone else
        except OSError:
            return False
        return True

    def probe(self) -> dict[str, object]:
        """Inspect the lock without acquiring: {'state': ...} where state is
        'free' | 'held' | 'stale'."""
        try:
            raw = self.path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return {"state": "free"}
        except OSError:
            return {"state": "stale"}
        try:
            pid = int(raw)
        except ValueError:
            return {"state": "stale"}
        if self._pid_alive(pid):
            return {"state": "held", "pid": pid}
        return {"state": "stale", "pid": pid}

    def acquire(self, force: bool = False) -> tuple[bool, str]:
        """Take the lock. Returns (ok, message).

        force=True replaces a held lock deliberately (--force); a stale
        lock is always replaced, forced or not.
        """
        if self._held:
            return True, "already held by this process"
        probe = self.probe()
        state = probe["state"]
        if state == "held" and not force:
            return False, (
                f"another fs-organizer (pid {probe['pid']}) is already running "
                f"for this config; stop it first or pass --force to override"
            )
        try:
            if state != "held" or force:
                # A stale/garbage lock file must be removed before the create
                # (its mere existence would fail the exclusive 'x' open).
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # 'x' mode: create fails if another process won the race.
            with self.path.open("x", encoding="utf-8") as fh:
                fh.write(str(os.getpid()))
            self._held = True
            return True, "acquired"
        except FileExistsError:
            # Lost a race with a concurrent acquirer.
            return False, "another process acquired the lock simultaneously"
        except OSError as exc:
            return False, f"could not create lock file {self.path}: {exc}"

    def release(self) -> None:
        """Remove our lock file. Never raises; a vanished file is fine."""
        if not self._held:
            return
        try:
            self.path.unlink()
        except OSError:
            pass
        self._held = False

    def __enter__(self) -> "InstanceLock":
        ok, msg = self.acquire()
        if not ok:
            raise RuntimeError(msg)
        return self

    def __exit__(self, *exc) -> None:
        self.release()
