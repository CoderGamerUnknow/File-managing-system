#!/usr/bin/env bash
# Provision a LOCAL Linux test box on WSL2 from a Windows machine — no Docker
# required — so every gate can be run on Linux without pushing anything.
#
#   scripts/setup_local_linux.sh                      # Ubuntu 24.04 (glibc)
#   scripts/setup_local_linux.sh --flavor alpine      # Alpine (musl)
#   scripts/setup_local_linux.sh --mirror <url>       # force a mirror
#   scripts/setup_local_linux.sh <wsl-path>           # or an explicit /mnt/c/... path
#
# Ubuntu is the default because it is what CI's ubuntu-latest runs: same glibc,
# same libc family, so a green run here means the same thing a green CI run
# means. Alpine stays available as an EXTRA portability check (musl), not as a
# stand-in for CI.
#
# Then (flavor=ubuntu, venv at /root/venv):
#   wsl -d fsorg-linux -u root -- sh -c 'cd /root/repo && /root/venv/bin/python -m pytest -q'
#
# Overrides:  FLAVOR, MIRROR (or ALPINE_MIRROR / UBUNTU_MIRROR), DISTRO.
# The rootfs tarball is cached under $HOME/wsl-distros/ and reused when its
# size still matches the server, so re-runs skip the slowest step.
set -euo pipefail

FLAVOR="${FLAVOR:-ubuntu}"
DISTRO="${DISTRO:-fsorg-linux}"
FORCE_MIRROR="${MIRROR:-}"
REPO_ARG=""

while [ $# -gt 0 ]; do
    case "$1" in
        --flavor) FLAVOR="$2"; shift 2 ;;
        --distro) DISTRO="$2"; shift 2 ;;
        --mirror) FORCE_MIRROR="$2"; shift 2 ;;
        -h|--help) sed -n '2,25p' "$0"; exit 0 ;;
        -*) echo "unknown option: $1" >&2; exit 2 ;;
        *)  REPO_ARG="$1"; shift ;;
    esac
done

case "$FLAVOR" in
    ubuntu) FLAVOR_MIRRORS=(
        "${UBUNTU_MIRROR:-https://mirrors.ustc.edu.cn/ubuntu-cd/24.04}"
        "http://archive.ubuntu.com/ubuntu"
        "https://mirrors.tuna.tsinghua.edu.cn/ubuntu"
        "https://mirrors.aliyun.com/ubuntu"
    ) ;;
    alpine) FLAVOR_MIRRORS=(
        "${ALPINE_MIRROR:-https://mirrors.aliyun.com/alpine}"
        "https://dl-cdn.alpinelinux.org/alpine"
        "https://mirrors.tuna.tsinghua.edu.cn/alpine"
        "https://mirrors.ustc.edu.cn/alpine"
    ) ;;
    *) echo "unknown flavor: $FLAVOR (want ubuntu or alpine)" >&2; exit 2 ;;
esac

# --- where the rootfs comes from -------------------------------------------
if [ "$FLAVOR" = ubuntu ]; then
    # The WSL rootfs image is published by Canonical on cloud-images, not by
    # the Ubuntu mirror network, so it is fetched from its canonical home and
    # the mirror list above is used for `apt` inside the distro instead.
    ROOTFS_URL="${UBUNTU_ROOTFS:-https://cloud-images.ubuntu.com/wsl/releases/24.04/current/ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz}"
else
    ROOTFS_URL=""   # resolved per-mirror below
fi

# Two different conversions are needed depending on what is passed to wsl.exe,
# and using the wrong one silently corrupts a path:
#
#   * paths meant FOR the guest (/mnt/c/...) must reach wsl.exe verbatim, so
#     MSYS's argument rewriting has to be switched off for those calls;
#   * paths meant for wsl.exe's OWN use (the --import install dir and rootfs,
#     which live under /c/... in Git Bash terms) must be rewritten to Windows
#     form, so rewriting must be left ON there.
#
# Leaving rewriting on for the first case produced "C:/Program Files/Git/mnt/…";
# leaving it off for the second made --import look for a literal "/c/…" and fail
# with "the system cannot find the path specified".
wsl_guest() {   # run a command in the distro; args keep their Linux form
    MSYS2_ARG_CONV_EXCL='*' MSYS_NO_PATHCONV=1 wsl.exe "$@"
}

win_path() {    # Git Bash /c/Users/x -> C:/Users/x (a form wsl.exe understands)
    case "$1" in
        /[a-z]/*) printf '%s:%s' "$(printf '%s' "${1:1:1}" | tr 'a-z' 'A-Z')" "${1:2}" ;;
        *) printf '%s' "$1" ;;
    esac
}

# Repo path in a form WSL can read: Git Bash prints /c/..., Windows prints
# C:\... , and WSL needs /mnt/c/... (lowercase drive — /mnt/C does not exist).
REPO="${REPO_ARG:-$(pwd)}"
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
command -v wsl.exe >/dev/null || { echo "wsl.exe not found — this script needs WSL2" >&2; exit 1; }
# Nothing else in this script needs the conversion disabled; drop any inherited
# setting so curl (and the rest) behave normally.
unset MSYS2_ARG_CONV_EXCL MSYS_NO_PATHCONV

# --- mirror probe ----------------------------------------------------------
# Measured, not assumed: dl-cdn.alpinelinux.org is a global CDN that can crawl
# to ~1 KB/s from some networks while a regional mirror does MB/s, and which
# one is fast depends on where you are. Probe each candidate and keep the
# fastest one that answers at all.
#
# The probe asks for a SMALL slice on purpose: the question is "does this
# mirror work, and roughly how fast", and a slice big enough to time out would
# report a slow-but-working mirror as dead (which is how a 1 KB/s mirror got
# mistaken for an unreachable one).
probe_speed() {   # $1 = url -> bytes/sec on stdout (0 when it does not answer)
    curl -o /dev/null -s -w '%{speed_download}' --max-time 6 -r 0-262143 "$1" 2>/dev/null || echo 0
}

# --mirror / $MIRROR replaces the probe: one candidate, no measuring. This has
# to rewrite FLAVOR_MIRRORS, since that is what pick_fastest is handed below.
if [ -n "$FORCE_MIRROR" ]; then
    FLAVOR_MIRRORS=("$FORCE_MIRROR")
    echo "== mirror forced: $FORCE_MIRROR"
fi

pick_fastest() {  # $1 = label, rest = candidate base urls; winner on stdout
    local label="$1"; shift
    local best="" best_speed=0 url speed first="$1"
    for url in "$@"; do
        speed="$(probe_speed "$url")"
        awk -v s="$speed" 'BEGIN{exit !(s+0 > 0)}' || speed=0
        # Progress goes to stderr so stdout carries only the winner.
        printf '   %9.1f KB/s  %s\n' "$(awk -v s="$speed" 'BEGIN{print s/1024}')" "$url" >&2
        if awk -v s="$speed" -v b="$best_speed" 'BEGIN{exit !(s+0 > b+0)}'; then
            best="$url"; best_speed="$speed"
        fi
    done
    if [ -z "$best" ]; then
        # Nothing answered inside the probe window. Fall back to the first
        # candidate rather than aborting: a slow mirror still installs, it
        # just installs slowly, and --mirror is there to override.
        echo "   (none answered the probe; falling back to $first)" >&2
        best="$first"
    fi
    echo "$label -> $best" >&2
    printf '%s' "$best"
}

if [ "$FLAVOR" = ubuntu ]; then
    APT_MIRROR="$(pick_fastest 'apt mirror' "${FLAVOR_MIRRORS[@]}")"
    DISTRO_ROOTFS="$HOME/wsl-distros/ubuntu-rootfs.tar.gz"
    echo "== rootfs: $ROOTFS_URL"
else
    ALPINE_MIRROR="$(pick_fastest 'alpine mirror' "${FLAVOR_MIRRORS[@]}")"
    # Resolve both the rootfs and the repo series from the mirror's index
    # instead of pinning a version: Alpine deletes the tarball of an EOL point
    # release from the mirrors, so a hardcoded 3.22.0 stops existing (404) a
    # few months after it is written. The series (v3.24) is what apk needs for
    # its repositories — 'main'/'community' without it do not resolve.
    ROOTFS_FILE="${ALPINE_ROOTFS:-$(curl -fsS --max-time 30 \
        "$ALPINE_MIRROR/latest-stable/releases/x86_64/" 2>/dev/null \
        | grep -o 'alpine-minirootfs-[0-9][0-9.]*-x86_64\.tar\.gz' | sort -V | tail -1)}"
    [ -n "$ROOTFS_FILE" ] || ROOTFS_FILE="alpine-minirootfs-3.24.2-x86_64.tar.gz"
    ALPINE_SERIES="v$(printf '%s' "$ROOTFS_FILE" | sed -E 's/^alpine-minirootfs-([0-9]+\.[0-9]+).*/\1/')"
    ROOTFS_URL="$ALPINE_MIRROR/latest-stable/releases/x86_64/$ROOTFS_FILE"
    DISTRO_ROOTFS="$HOME/wsl-distros/alpine-rootfs.tar.gz"
    echo "== rootfs: $ROOTFS_URL"
fi

# --- fetch (cached) --------------------------------------------------------
mkdir -p "$HOME/wsl-distros"
fetch_rootfs() {   # $1 = url, $2 = destination
    local url="$1" dest="$2" want have
    want="$(curl -fsIL --max-time 60 "$url" 2>/dev/null | tr -d '\r' \
        | awk 'tolower($1)=="content-length:"{n=$2} END{print n+0}')"
    if [ -f "$dest" ] && [ "$want" -gt 0 ]; then
        have="$(wc -c < "$dest" | tr -d ' ')"
        if [ "$have" = "$want" ]; then
            echo "== rootfs cached ($(awk -v b="$have" 'BEGIN{printf "%.0f", b/1048576}') MiB), skipping download"
            return 0
        fi
        echo "== cached rootfs is $have bytes, server says $want — refetching"
    fi
    # A slow link times out mid-transfer; resume and retry rather than restart.
    # --speed-limit aborts a stalled connection so the retry loop can proceed.
    local attempt
    for attempt in 1 2 3 4 5 6; do
        curl -fL -C - --speed-limit 20000 --speed-time 30 --retry 2 \
            --progress-bar -o "$dest.part" "$url" && break
        echo "== download attempt $attempt incomplete ($(wc -c < "$dest.part" 2>/dev/null || echo 0) bytes) — resuming"
    done
    mv "$dest.part" "$dest"
}

echo "== rebuilding $DISTRO from $ROOTFS_URL"
fetch_rootfs "$ROOTFS_URL" "$DISTRO_ROOTFS"

wsl.exe --unregister "$DISTRO" >/dev/null 2>&1 || true
INSTALL_DIR="$HOME/wsl-distros/$DISTRO"
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
wsl.exe --import "$DISTRO" "$(win_path "$INSTALL_DIR")" "$(win_path "$DISTRO_ROOTFS")" --version 2

# --- toolchain -------------------------------------------------------------
echo "== installing python toolchain ($FLAVOR)"
if [ "$FLAVOR" = ubuntu ]; then
    # PEP 668 marks the system interpreter as externally managed; the venv at
    # the end of this script is what the gates actually use, so only the
    # toolchain install happens here.
    wsl_guest -d "$DISTRO" -u root -- bash -c "
        set -e
        export DEBIAN_FRONTEND=noninteractive
        printf 'deb ${APT_MIRROR} noble main universe\n' > /etc/apt/sources.list
        rm -f /etc/apt/sources.list.d/*.sources
        apt-get update -qq
        apt-get install -y -qq python3 python3-venv python3-pip \
            build-essential ca-certificates
        python3 --version
    "
else
    wsl_guest -d "$DISTRO" -u root -- sh -c "
        set -e
        printf '%s/%s/main\n%s/%s/community\n' '$ALPINE_MIRROR' '$ALPINE_SERIES' '$ALPINE_MIRROR' '$ALPINE_SERIES' > /etc/apk/repositories
        apk update -q
        apk add --no-cache -q python3 py3-pip gcc musl-dev linux-headers make \
            ca-certificates libffi-dev openssl-dev zlib-dev bash
        python3 --version
    "
fi

# --- repo + dev deps -------------------------------------------------------
echo "== copying the repo from $REPO"
# `sh`, not `bash`: the Alpine minirootfs has no bash installed at this point.
wsl_guest -d "$DISTRO" -u root -- sh -c "
    set -e
    rm -rf /root/repo
    mkdir -p /root/repo
    cp -r '$REPO/.' /root/repo/
    cd /root/repo
    python3 -m venv /root/venv
    # pip's 15s default timeout is too short for a slow link: the /simple/ page
    # for ruff alone is ~800 KB, and a truncated page reads as
    # 'no matching distribution' rather than as a timeout.
    PIP_DEFAULT_TIMEOUT=180 /root/venv/bin/python -m pip install -q --upgrade pip
    PIP_DEFAULT_TIMEOUT=180 /root/venv/bin/python -m pip install -q -e '.[dev]'
    # build/twine are NOT in the [dev] extra on purpose — CI installs them in
    # the job that needs them — but the local box exists to run EVERY gate,
    # and 'python -m build' is one of the documented ones.
    PIP_DEFAULT_TIMEOUT=180 /root/venv/bin/python -m pip install -q build twine
    echo INSTALL_OK
"

cat <<EOF

Ready. Run the gates on Linux with (venv at /root/venv):

On CMD or PowerShell:
  wsl -d $DISTRO -u root -- sh -c 'cd /root/repo && /root/venv/bin/python -m ruff check .'
  wsl -d $DISTRO -u root -- sh -c 'cd /root/repo && /root/venv/bin/python -m pytest -q'
  wsl -d $DISTRO -u root -- sh -c 'cd /root/repo && /root/venv/bin/python scripts/smoke_dryrun.py'
  wsl -d $DISTRO -u root -- sh -c 'cd /root/repo && /root/venv/bin/python scripts/smoke_ui.py'

And the artifact gates (needs the build/twine this script installed):
  wsl -d $DISTRO -u root -- sh -c 'cd /root/repo && /root/venv/bin/python -m build'
  wsl -d $DISTRO -u root -- bash scripts/verify_artifact.sh 'dist/*.whl' wheel
  wsl -d $DISTRO -u root -- bash scripts/verify_artifact.sh 'dist/*.tar.gz' sdist

In Git Bash prefix each with MSYS2_ARG_CONV_EXCL='*' so the /mnt paths survive.
EOF
