# Releasing fs-organizer

Everything below is driven by `.github/workflows/release.yml` and
`.github/workflows/ci.yml`. No release step needs a human-built artifact.

## Cutting a release

1. Land the changes on `main` with CI green.
2. Bump the version in **both** places (they must agree):
   - `pyproject.toml` → `version = "X.Y.Z"`
   - `fs_organizer/__init__.py` → `__version__ = "X.Y.Z"`
3. Move the `[Unreleased]` section of `CHANGELOG.md` to a dated
   `[X.Y.Z]` section and add its link next to the others at the bottom:
   `[X.Y.Z]: https://github.com/CoderGamerUnknow/File-managing-system/releases/tag/vX.Y.Z`
4. Commit, then tag and push:

   ```bash
   git commit -am "Release vX.Y.Z"
   git tag -a vX.Y.Z -m "vX.Y.Z — <headline>"
   git push origin main vX.Y.Z
   ```

The tag push starts the Release workflow; a few minutes later the release
exists with the wheel and sdist attached.

## What a tag push runs

| Job | Purpose |
| --- | --- |
| `resolve` | Confirms HEAD is *exactly* the tag (fails instead of packaging the wrong commit). |
| `test` | Linux quality gate on the source tree: `ruff check`, the full pytest suite, `smoke_dryrun.py`, `smoke_ui.py`. |
| `build` | `python -m build` (sdist + wheel) + `twine check --strict`; uploads them as a workflow artifact. |
| `verify` | **ubuntu and windows**: installs the wheel *and* the sdist (real source build) into fresh venvs and runs the full suite against the installed package (`scripts/verify_artifact.sh`). |
| `attach` | Only after `verify`: creates the release with generated notes if missing, then `gh release upload --clobber` (re-runs are idempotent). |
| `publish` | PyPI via trusted publishing. **Skipped** until the repository variable `PYPI_PUBLISH` is `true` (see below), so tags stay green before setup. |

`ci.yml` also runs on `v*` tag pushes, so the full matrix
(ubuntu py3.9 / ubuntu py3.13 / windows py3.13) gates the release too.

## Re-running or back-filling an existing tag

**Actions → Release → Run workflow**, set the `tag` input (e.g. `v0.4.1`).
The whole pipeline re-runs against that tag; assets are replaced with
`--clobber`, so it is safe to repeat.

## On-demand CI for any ref

**Actions → CI → Run workflow** (no inputs — it uses the branch/commit you
select in the UI, or the API's `ref`). This gives a fresh
ubuntu-py3.9 / ubuntu-py3.13 / windows-py3.13 run **plus** the artifacts
matrix for that exact commit — this is how a Windows dev machine gets a
Linux result without pushing anything.

Via the API:

```bash
gh workflow run CI --ref <branch-or-sha>
gh run watch          # or: gh run list
```

## Verifying artifacts locally

```bash
python -m build
bash scripts/verify_artifact.sh "dist/*.whl"  wheel
bash scripts/verify_artifact.sh "dist/*.tar.gz" sdist
```

Each installs the artifact into a fresh `venv-<label>/` and runs the whole
suite against it from a temp directory, so the checkout cannot shadow
site-packages. Works in Git Bash on Windows and in any Linux shell — and on
Linux from a Windows box via `scripts/setup_local_linux.sh` (see
`docs/TESTING.md`), which provisions the same glibc userland CI runs.

## Enabling PyPI publishing (one-time, needs your PyPI login)

Check what is left first:

```bash
python scripts/check_publish_ready.py                    # offline checks
GITHUB_TOKEN=… python scripts/check_publish_ready.py     # + the repo variable via API
```

It verifies everything the repository controls (workflow name, the
`PYPI_PUBLISH` gate, `id-token: write`, that no API token is stored in the
workflow, and that `pyproject.toml` and `fs_organizer/__init__.py` agree on
the version), then prints the exact values for the one step that cannot be
automated. Two steps remain, both required; until they are done the `publish`
job is skipped and nothing breaks.

1. **On pypi.org** — create the project if it does not exist yet, then
   *Account → Publishing → Add a new pending publisher*:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `fs-organizer` |
   | Owner | `CoderGamerUnknow` |
   | Repository name | `File-managing-system` |
   | Workflow name | `release.yml` |
   | Environment name | *(leave empty)* |

2. **On GitHub** — *Settings → Secrets and variables → Actions →
   Variables → New repository variable*:
   `PYPI_PUBLISH` = `true`

The next `v*` tag will publish to PyPI with a short-lived OIDC token — no
API key is stored in the repository. To publish an existing tag without a
new one, run the Release workflow manually for that tag (the `publish` job
will then run too).

> Step 1 requires your pypi.org login, so it cannot be automated from the
> repository side. Everything else is verified by the preflight above.
