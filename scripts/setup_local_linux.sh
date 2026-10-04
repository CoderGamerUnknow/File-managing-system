#!/usr/bin/env bash
# Provision a LOCAL Linux test box (Alpine on WSL2) on a Windows machine —
# no Docker required — so the suite can be run on Linux without pushing.
#
#   scripts/setup_local_linux.sh            # from the repo root (Git Bash)
#   scripts/setup_local_linux.sh <wsl-path> # or an explicit /mnt/c/... path
#
# Then:
#   wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && python3 -m pytest -q'
#
# See docs/TESTING.md. The distro is rebuilt from scratch every run (idempotent).
set -euo pipefail

DISTRO=fsorg-linux
# Fast mirror: dl-cdn.alpinelinux.org was measured at ~3 KB/s from some
# networks while this mirror does 150-500 KB/s. Change if yours differs.
MIRROR="${ALPINE_MIRROR:-https://mirrors.tuna.tsinghua.edu.cn/alpine/v3.22}"
ROOTFS="$MIRROR/releases/x86_64/alpine-minirootfs-3.22.0-x86_64.tar.gz"

# Repo path in a form WSL can read: Git Bash prints /c/..., Windows prints
# C:\... , and WSL needs /mnt/c/... (lowercase drive — /mnt/C does not exist).
REPO="${1:-$(pwd)}"
REPO="${REPO//\\//}"          # C:\Users\x -> C:/Users/x
case "$REPO" in
    [A-Za-z]:/*)
        drive="$(printf '%s' "${REPO:0:1}" | tr 'A-Z' 'a-z')"
        REPO="/mnt/$drive${REPO:2}"   # C:/Users/x -> /mnt/c/Users/x
        ;;
    /[a-z]/*)
        REPO="/mnt/${REPO#/}"         # /c/Users/x -> /mnt/c/Users/x
        ;;
esac
echo "== repo source: $REPO"

echo "== rebuilding $DISTRO from $ROOTFS"
wsl --unregister "$DISTRO" 2>/dev/null || true
mkdir -p "$HOME/wsl-distros/$DISTRO"
curl -fL --retry 3 -o "$HOME/wsl-distros/alpine-rootfs.tar.gz" "$ROOTFS"
wsl --import "$DISTRO" "$HOME/wsl-distros/$DISTRO" \
    "$HOME/wsl-distros/alpine-rootfs.tar.gz" --version 2

echo "== installing python toolchain"
wsl -d "$DISTRO" -u root -- sh -c "
    set -e
    printf '%s/main\n%s/community\n' '$MIRROR' '$MIRROR' > /etc/apk/repositories
    apk update -q
    apk add --no-cache -q python3 py3-pip gcc musl-dev linux-headers make \
        ca-certificates libffi-dev openssl-dev zlib-dev
    python3 --version
"

echo "== copying the repo from $REPO"
wsl -d "$DISTRO" -u root -- sh -c "
    set -e
    rm -rf /root/repo
    mkdir -p /root/repo
    cp -r '$REPO/.' /root/repo/
    cd /root/repo
    python3 -m pip install -q --break-system-packages --upgrade pip
    python3 -m pip install -q --break-system-packages -e '.[dev]'
    echo INSTALL_OK
"

cat <<'EOF'

Ready. Run the gates on Linux with:
  wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && python3 -m ruff check .'
  wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && python3 -m pytest -q'
  wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && python3 scripts/smoke_dryrun.py'
  wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && python3 scripts/smoke_ui.py'
EOF
