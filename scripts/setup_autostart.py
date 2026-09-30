"""One-time setup: live config + auto-start on login (Windows).

Creates ~/.fs-organizer/config.json watching your user folders and registers
a registry Run key so fs-organizer starts hidden in the background at every
login (pythonw.exe, no console window).

Usage:
  python scripts/setup_autostart.py           # install config + autostart
  python scripts/setup_autostart.py --remove  # remove the autostart entry
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

APP_DIR = Path.home() / ".fs-organizer"
CONFIG_PATH = APP_DIR / "config.json"
LOG_PATH = APP_DIR / "fs-organizer.log"
RUN_KEY_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "FSOrganizer"

# User-profile folders to watch. "All except system32": we deliberately watch
# only the folders you actually use, never Windows/Program Files — moving
# DLLs/EXEs out of app folders breaks software, so those stay off-limits.
# Shell-folder registry value names (known-folder redirections like OneDrive
# are resolved here so we watch the REAL locations).
SHELL_FOLDER_NAMES = {
    "Desktop": "Desktop",
    "Documents": "Personal",
    "Downloads": "{374DE290-123F-4565-9164-39C4925E467B}",
    "Pictures": "My Pictures",
    "Music": "My Music",
    "Videos": "My Video",
}
ALWAYS_EXCLUDE = ["appdata"]  # app-internal data, not user files

EXTRA_RULES = {
    ".doc": "Documents", ".xls": "Documents", ".ppt": "Documents",
    ".csv": "Documents", ".rtf": "Documents", ".heic": "Images",
    ".rar": "Archives",
}

EXTRA_IGNORES = ["~$*", "~*.docx", "~*.xlsx", "~*.pptx"]  # Office lock files


def _known_folder(registry_value: str) -> Path | None:
    """Resolve a shell known-folder (handles OneDrive redirections)."""
    try:
        import winreg

        key_path = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            raw, _ = winreg.QueryValueEx(key, registry_value)
        return Path(os.path.expandvars(raw))
    except OSError:
        return None


def discover_watch_folders() -> list[str]:
    home = Path.home()
    folders: list[str] = []
    for fallback_name, registry_value in SHELL_FOLDER_NAMES.items():
        p = _known_folder(registry_value) or home / fallback_name
        if p.is_dir():
            folders.append(str(p))
    # Never watch app-data or system areas; dedupe while keeping order.
    seen: set[str] = set()
    return [
        f for f in folders
        if not any(part in f.lower() for part in ALWAYS_EXCLUDE)
        and not (f in seen or seen.add(f))
    ]


def build_or_update_config() -> dict:
    watch_folders = discover_watch_folders()
    if not watch_folders:
        sys.exit("No user folders found to watch — nothing to do.")

    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        print(f"Updating existing config: {CONFIG_PATH}")
    else:
        from fs_organizer.config import DEFAULT_IGNORE_PATTERNS, DEFAULT_TARGET_RULES

        rules = dict(DEFAULT_TARGET_RULES)
        rules.update(EXTRA_RULES)
        cfg = {
            "target_rules": rules,
            "ignore_patterns": DEFAULT_IGNORE_PATTERNS + EXTRA_IGNORES,
            "use_date_subfolders": False,
            "dry_run": False,
            "file_stable_seconds": 1.0,
            "ai": {"enabled": False},
        }
        print(f"Creating new config: {CONFIG_PATH}")

    cfg["watch_folders"] = watch_folders
    cfg["target_root"] = str(Path.home() / "Organized")
    return cfg


def find_pythonw() -> str | None:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        return str(pythonw)
    # venv-style layouts may keep pythonw elsewhere; fall back to python.
    python = Path(sys.executable)
    return str(python) if python.exists() else None


def install_autostart() -> None:
    import winreg

    pythonw = find_pythonw()
    if not pythonw:
        sys.exit("Could not locate pythonw.exe/python.exe next to the current interpreter.")
    package_dir = Path(__file__).resolve().parent.parent
    command = f'"{pythonw}" -m fs_organizer "{CONFIG_PATH}" --log-file "{LOG_PATH}"'
    if "pythonw" not in pythonw.lower():
        print("WARNING: pythonw.exe not found; using python.exe (a console window will appear).")

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command)
    print("Auto-start registered (HKCU\\...\\Run).")
    print(f"  Command: {command}")


def remove_autostart() -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY_PATH, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE_NAME)
        print("Auto-start entry removed.")
    except FileNotFoundError:
        print("No auto-start entry found (already removed).")


def ensure_fs_organizer_importable() -> None:
    """Make sure 'python -m fs_organizer' works from anywhere."""
    try:
        import fs_organizer  # noqa: F401
    except ImportError:
        sys.exit("fs_organizer is not importable. Run:  pip install .")


def main() -> int:
    if "--remove" in sys.argv:
        remove_autostart()
        return 0

    ensure_fs_organizer_importable()
    APP_DIR.mkdir(exist_ok=True)

    cfg = build_or_update_config()
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    print(f"  Watching: {', '.join(cfg['watch_folders'])}")
    print(f"  Target root: {cfg['target_root']}")
    print(f"  Logs: {LOG_PATH}")

    install_autostart()
    print("\nDone. It will start hidden at your next login.")
    print(f"Remove later with: python \"{Path(__file__)}\" --remove")
    return 0


if __name__ == "__main__":
    sys.exit(main())
