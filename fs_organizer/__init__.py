"""fs-organizer package.

Public surface (everything else is internal implementation detail):

- :func:`fs_organizer.mover.plan` / :func:`fs_organizer.mover.plan_actions` -- dry-run pre-scans for dashboards
- :func:`fs_organizer.ui.files_payload` / :func:`fs_organizer.ui.plan_summary` / :func:`fs_organizer.ui.rules_payload` -- dashboard views against a resolved ``Config``
- :func:`fs_organizer.diagnostics.watch_diag` / :func:`fs_organizer.diagnostics.check_report` -- ``check``/``watch-diag`` data and renderings
- :func:`fs_organizer.config.Config.expanded_watch_folders` / :func:`fs_organizer.config.Config.effective_ignore_patterns` / :func:`fs_organizer.config.Config.coverage_report` -- resolved configuration metadata
"""

from . import ai
from . import config
from . import diagnostics
from . import mover
from . import pool
from . import rules
from . import ui
from . import views
from . import watcher

__all__ = [
    "ai",
    "config",
    "diagnostics",
    "mover",
    "pool",
    "rules",
    "ui",
    "views",
    "watcher",
]

__version__ = "0.2.0"

