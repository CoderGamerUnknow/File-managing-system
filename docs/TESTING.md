# Testing fs-organizer

## Local gates (any OS)

```bash
ruff check .                 # lint (also enforced in CI)
pytest -q                    # full suite (~515 tests)
python scripts/smoke_dryrun.py
python scripts/smoke_ui.py

# Test what actually ships (build + install + suite against the artifact)
python -m build
bash scripts/verify_artifact.sh "dist/*.whl"  wheel
bash scripts/verify_artifact.sh "dist/*.tar.gz" sdist
```

## Running the suite on Linux from a Windows machine

No Docker needed: `scripts/setup_local_linux.sh` (run from Git Bash at the
repo root) provisions an **Alpine + Python 3.12** distro on WSL2, copies the
repo into it, and installs the dev dependencies. It rebuilds the distro from
scratch each run, so it is idempotent:

```bash
scripts/setup_local_linux.sh
```

Then every gate runs inside that Linux:

```bash
wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && python3 -m ruff check .'
wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && python3 -m pytest -q'
wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && python3 scripts/smoke_dryrun.py'
wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && python3 scripts/smoke_ui.py'
```

Notes:

- The default Alpine mirror (`ALPINE_MIRROR` overrides it) is picked for
  speed; `dl-cdn.alpinelinux.org` measured ~3 KB/s on some networks while
  the default mirror does 150–500 KB/s.
- Alpine is **musl** libc, which is *not* what CI's `ubuntu-latest` runs —
  it is an extra portability check on top of CI, not a replacement for it.
  CI remains the authority for glibc (`ubuntu-latest`, py3.9 + py3.13) and
  for Windows (`windows-latest`, py3.13).
- To drop the distro: `wsl --unregister fsorg-linux`.

## On-demand CI for any ref

**Actions → CI → Run workflow** gives a fresh run of the full matrix
(ubuntu py3.9 / ubuntu py3.13 / windows py3.13) **plus** the `artifacts`
jobs (build + install each artifact + suite against it) for the selected
branch or commit — no push required. See `docs/RELEASING.md`.

## What CI runs

| Workflow job | What it proves |
| --- | --- |
| `test` (3× matrix) | lint + suite + both smokes on the source tree, on glibc and Windows |
| `artifacts` (2× matrix) | the built wheel **and** sdist install cleanly and pass the whole suite on ubuntu and windows |
| release `verify` (2× matrix) | same, against the artifacts built from the tagged code, before assets are attached |
| release `publish` | PyPI upload via trusted publishing — skipped until `PYPI_PUBLISH=true` |
