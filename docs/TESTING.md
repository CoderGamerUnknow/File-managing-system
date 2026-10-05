# Testing fs-organizer

## Local gates (any OS)

```bash
ruff check .                 # lint (also enforced in CI)
pytest -q                    # full suite (518 tests; a few skip per platform)
python scripts/smoke_dryrun.py
python scripts/smoke_ui.py

# Test what actually ships (build + install + suite against the artifact)
python -m build
bash scripts/verify_artifact.sh "dist/*.whl"  wheel
bash scripts/verify_artifact.sh "dist/*.tar.gz" sdist
```

## Running the suite on Linux from a Windows machine

No Docker needed: `scripts/setup_local_linux.sh` (run from Git Bash at the
repo root) provisions a WSL2 distro, copies the repo into it, and installs
the dev dependencies into a venv at `/root/venv`. It rebuilds the distro from
scratch each run, so it is idempotent:

```bash
scripts/setup_local_linux.sh                  # Ubuntu 24.04 (glibc) — same as CI
scripts/setup_local_linux.sh --flavor alpine  # Alpine (musl) — extra portability check
```

Then every gate runs inside that Linux (venv at `/root/venv`):

```bash
wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && /root/venv/bin/python -m ruff check .'
wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && /root/venv/bin/python -m pytest -q'
wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && /root/venv/bin/python scripts/smoke_dryrun.py'
wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && /root/venv/bin/python scripts/smoke_ui.py'
```

In Git Bash, prefix those with `MSYS2_ARG_CONV_EXCL='*'` so the `/mnt/c/...`
paths reach the guest unchanged; from CMD or PowerShell they work as-is.

Notes:

- **Ubuntu is the default because CI runs `ubuntu-latest`.** A local musl run
  is a different libc family from the one CI tests, so it could never stand in
  for a CI result. `--flavor alpine` is still worth running as an *extra*
  portability check, just not as the primary local gate.
- **Mirrors are measured, not guessed.** The script issues a small ranged
  request against each candidate and keeps the fastest; on the network this
  was last set up on, `dl-cdn.alpinelinux.org` and three regional mirrors all
  measured ~0.1 KB/s while the rootfs host did MB/s. Which mirror is fast
  depends on where you are, so nothing is hardcoded: override with
  `--mirror`, `ALPINE_MIRROR` or `UBUNTU_MIRROR` if the probe picks wrong.
- The rootfs and the Alpine repository series are resolved from the mirror's
  index, not pinned — a pinned version disappears from the mirrors once it is
  EOL, which is exactly what happened to `alpine-minirootfs-3.22.0`.
- The rootfs tarball is cached in `$HOME/wsl-distros/` and reused when its
  size still matches the server, so re-runs skip the download.
- To drop a distro: `wsl --unregister fsorg-linux`.

## Checking the PyPI path

```bash
python scripts/check_publish_ready.py    # add GITHUB_TOKEN=… to check the repo variable too
```

Validates everything the repository controls about publishing and prints the
exact values for the one step that needs your pypi.org login. See
`docs/RELEASING.md`.

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

## Verified locally

Every gate listed above, on this machine. The columns are separate runs on
separate operating systems, so the counts differ where the platforms do:

| | Windows 11 · py3.13 | Ubuntu 24.04 (glibc) | Alpine (musl) |
| --- | --- | --- | --- |
| `ruff check` | clean | clean | clean |
| `pytest -q` | 517 passed / 1 skipped | 514 passed / 4 skipped | 512 passed / 4 skipped |
| `smoke_dryrun.py` / `smoke_ui.py` | pass | pass | pass |
| `build` + `twine check --strict` | pass | — | — |
| wheel + sdist install-and-test | pass | — | — |

Two caveats, so the Linux columns are not read as more than they are: they
were recorded on runs made before the final release commit (only the Windows
column was re-run afterwards), and the Alpine column comes from the musl pass
made before the two `age_policy` regression tests landed — hence its total two
tests lower. The suite collects 518 tests and each platform skips a different
handful of them (Windows 1, glibc 4).

Running the glibc suite 8× in a row is what surfaced the `age_policy`
negative-age bug (roughly 1 run in 5); see the changelog.
