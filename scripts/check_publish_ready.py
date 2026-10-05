#!/usr/bin/env python3
"""Preflight for the PyPI publishing path.

Everything in this repository that *has* to be true before a tag can publish to
PyPI is checked here: the workflow shape, the version agreement, and (when a
token is available) the repository variable that arms the ``publish`` job.

The only remaining step is a form on pypi.org, which needs the owner's login and
therefore cannot be done from the repository side. This script prints the exact
field values for that form so nothing has to be looked up by hand.

    python scripts/check_publish_ready.py            # offline checks
    GITHUB_TOKEN=... python scripts/check_publish_ready.py   # + repo variable

Exit code 0 means the repository side is ready; 1 means something is wrong with
it. "Needs a human on pypi.org" is not a failure and does not change the exit
code — it is reported as an explicit PENDING step.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RELEASE_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release.yml"

# Must match docs/RELEASING.md and the GitHub remote.
EXPECTED_OWNER = "CoderGamerUnknow"
EXPECTED_REPO = "File-managing-system"
EXPECTED_WORKFLOW_FILE = "release.yml"
EXPECTED_PYPI_PROJECT = "fs-organizer"

# The PyPI form wants the workflow *file name*, which is what GitHub shows in
# the trusted-publisher dialog as "Workflow name".
PYPI_URL = "https://pypi.org/manage/account/publishing/"
GITHUB_VAR_URL = (
    f"https://github.com/{EXPECTED_OWNER}/{EXPECTED_REPO}"
    "/settings/variables/actions/new"
)


class Report:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.pending: list[str] = []

    def ok(self, msg: str) -> None:
        print(f"  PASS  {msg}")

    def fail(self, msg: str) -> None:
        self.failures.append(msg)
        print(f"  FAIL  {msg}")

    def pending_step(self, msg: str) -> None:
        self.pending.append(msg)
        print(f"  PENDING  {msg}")

    def note(self, msg: str) -> None:
        print(f"        {msg}")


def _workflow_text() -> str:
    return RELEASE_WORKFLOW.read_text(encoding="utf-8")


def _check_workflow_shape(report: Report) -> None:
    text = _workflow_text()

    if not re.search(r"^name:\s*Release\s*$", text, re.MULTILINE):
        report.fail("release.yml does not declare `name: Release` at the top")
    else:
        report.ok("workflow name is `Release` (what PyPI's form asks for)")

    # The publish job must be gated on the variable, or tags would try to
    # publish before the account side exists.
    if re.search(r"if:\s*vars\.PYPI_PUBLISH\s*==\s*'true'", text):
        report.ok("publish job is gated on `vars.PYPI_PUBLISH == 'true'`")
    else:
        report.fail("publish job is not gated on vars.PYPI_PUBLISH == 'true'")

    if re.search(r"id-token:\s*write", text):
        report.ok("OIDC permission (`id-token: write`) is granted")
    else:
        report.fail("publish job is missing `id-token: write`")

    if "pypa/gh-action-pypi-publish" in text:
        report.ok("uses pypa/gh-action-pypi-publish (trusted publishing, no token)")
    else:
        report.fail("publish job does not use pypa/gh-action-pypi-publish")

    # An API token in the repo would defeat the point of trusted publishing.
    for marker in ("PYPI_API_TOKEN", "TWINE_PASSWORD", "password:"):
        if marker in text:
            report.fail(f"release.yml mentions `{marker}`; trusted publishing needs no secret")
            break
    else:
        report.ok("no PyPI API token / password anywhere in release.yml")


def _declared_version() -> str | None:
    pyproject = (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE)
    return match.group(1) if match else None


def _package_version() -> str | None:
    init = (REPO_ROOT / "fs_organizer" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.MULTILINE)
    return match.group(1) if match else None


def _check_versions(report: Report) -> None:
    declared = _declared_version()
    package = _package_version()
    if declared is None or package is None:
        report.fail("could not read the version from pyproject.toml / __init__.py")
        return
    if declared != package:
        report.fail(f"version mismatch: pyproject.toml={declared}, __init__.py={package}")
        return
    report.ok(f"versions agree ({declared}) — this is the version PyPI would get")


def _check_remote(report: Report) -> None:
    git_dir = REPO_ROOT / ".git"
    if not git_dir.exists():
        report.note("not a git checkout; skipping remote check")
        return
    config = git_dir / "config"
    if not config.exists():
        return
    text = config.read_text(encoding="utf-8", errors="replace")
    if EXPECTED_OWNER not in text or EXPECTED_REPO not in text:
        report.fail(
            f"git remote does not point at {EXPECTED_OWNER}/{EXPECTED_REPO}; "
            "the PyPI pending publisher must match the real remote"
        )
        return
    report.ok(f"git remote is {EXPECTED_OWNER}/{EXPECTED_REPO}")


def _repo_variable(report: Report) -> None:
    """Read the repository variable through the API when a token is available."""
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        report.note(
            "no GITHUB_TOKEN/GH_TOKEN in the environment — repository variable "
            "PYPI_PUBLISH not checked (set one to include it)"
        )
        report.pending_step(
            f"set repository variable PYPI_PUBLISH=true at {GITHUB_VAR_URL}"
        )
        return

    url = (
        f"https://api.github.com/repos/{EXPECTED_OWNER}/{EXPECTED_REPO}"
        "/actions/variables/PYPI_PUBLISH"
    )
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            report.fail(
                "repository variable PYPI_PUBLISH does not exist, so publish is "
                f"skipped — create it at {GITHUB_VAR_URL}"
            )
        else:
            report.fail(f"GitHub API returned HTTP {exc.code} reading PYPI_PUBLISH")
        return
    except urllib.error.URLError as exc:
        report.fail(f"could not reach api.github.com: {exc.reason}")
        return

    value = str(payload.get("value", ""))
    if value == "true":
        report.ok("repository variable PYPI_PUBLISH is already `true`")
    else:
        report.fail(f"PYPI_PUBLISH is {value!r}, expected 'true'")


def main() -> int:
    print("PyPI publish preflight")
    print(f"  project: {EXPECTED_PYPI_PROJECT}   repo: {EXPECTED_OWNER}/{EXPECTED_REPO}")
    print()

    report = Report()

    print("workflow")
    _check_workflow_shape(report)
    print()
    print("release state")
    _check_versions(report)
    _check_remote(report)
    print()
    print("arming the publish job")
    _repo_variable(report)
    print()

    print("Pending publisher registration (pypi.org — needs your login):")
    report.pending_step(f"open {PYPI_URL} and add a pending publisher")
    report.note(f"  PyPI project name : {EXPECTED_PYPI_PROJECT}")
    report.note(f"  Owner             : {EXPECTED_OWNER}")
    report.note(f"  Repository name   : {EXPECTED_REPO}")
    report.note(f"  Workflow name     : {EXPECTED_WORKFLOW_FILE}")
    report.note("  Environment name  : (leave empty)")
    print()

    if report.failures:
        print(f"RESULT: {len(report.failures)} repository-side problem(s):")
        for failure in report.failures:
            print(f"  - {failure}")
        return 1
    if report.pending:
        print("RESULT: repository side is ready.")
        print(f"        {len(report.pending)} step(s) need your pypi.org login, then the")
        print("        next `v*` tag publishes with a short-lived OIDC token.")
        return 0
    print("RESULT: fully armed — the next `v*` tag publishes to PyPI.")
    return 0


if __name__ == "__main__":
    sys.exit(main())