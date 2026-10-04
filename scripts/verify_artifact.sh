#!/usr/bin/env bash
# Verify a built artifact exactly the way a user would consume it.
#
#   scripts/verify_artifact.sh <artifact-glob> <label>
#   scripts/verify_artifact.sh "dist/*.whl"  wheel
#   scripts/verify_artifact.sh "dist/*.tar.gz" sdist
#
# For each artifact this:
#   1. installs it into a FRESH venv (the sdist goes through a source build),
#   2. runs the FULL test suite against the *installed* package — the tests
#      are staged into a temp dir outside the repo, so the source tree can
#      never shadow site-packages and a green run really means the artifact
#      works,
#   3. checks the packaged data file (dashboard.html) and the console script.
#
# Runs on Linux and Windows runners (Git Bash) and on a dev machine.
# Usage inside CI: `bash scripts/verify_artifact.sh "dist/*.whl" wheel`
set -euo pipefail

GLOB="${1:?usage: verify_artifact.sh <artifact-glob> <label>}"
LABEL="${2:?usage: verify_artifact.sh <artifact-glob> <label>}"

# Fail with a clear message when the glob matched nothing (Windows bash
# leaves an unmatched pattern literal instead of erroring).
if [ ! -e $GLOB ]; then
    echo "No artifact matches: $GLOB" >&2
    exit 1
fi

echo "== verifying $LABEL: $GLOB"
python -m venv "venv-$LABEL"

# venv layout differs by platform (bin/ on POSIX, Scripts/ on Windows).
PY="$(pwd)/venv-$LABEL/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(pwd)/venv-$LABEL/Scripts/python.exe"
fi

"$PY" -m pip install --quiet --upgrade pip
# pytest is tooling; the package itself comes ONLY from the artifact.
"$PY" -m pip install --quiet $GLOB pytest

echo "-- installed distribution:"
"$PY" -m pip show fs-organizer | sed -n '1,3p'

# Stage the suite outside the repo: cwd has no fs_organizer/, so every
# `import fs_organizer` must resolve to site-packages.
STAGE="$(mktemp -d)"
rm -rf "$STAGE/tests"
cp -r tests "$STAGE/tests"
echo "-- running full suite against the installed $LABEL"
(cd "$STAGE" && "$PY" -m pytest -q tests)

echo "-- packaged data + console script:"
# Run from the staging dir: with the repo as cwd, sys.path[0] would be the
# source tree and this check would import fs_organizer from the checkout
# instead of from the install it is supposed to be validating.
(cd "$STAGE" && "$PY" - <<'PYEOF'
import pathlib
import fs_organizer

pkg = pathlib.Path(fs_organizer.__file__).parent
assert (pkg / "dashboard.html").exists(), "dashboard.html missing from the artifact"
normalized = str(pkg).replace("\\", "/")
assert "site-packages" in normalized, f"imported from {pkg}, not the install"
print("imported", fs_organizer.__version__, "from", pkg)
PYEOF
)

CLI="$(pwd)/venv-$LABEL/bin/fs-organizer"
if [ ! -x "$CLI" ]; then
    CLI="$(pwd)/venv-$LABEL/Scripts/fs-organizer.exe"
fi
"$CLI" --help >/dev/null
echo "== $LABEL OK"
