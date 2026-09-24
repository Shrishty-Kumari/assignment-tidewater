#!/usr/bin/env bash
# Installs the tools needed by `make up` and `make deploy` on Linux.
# Idempotent: only tools that are missing are installed.
#
#   scripts/install-prereqs.sh            (make prereqs; also run by make up)
set -euo pipefail

log()  { printf '\033[1;34m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }
has()  { command -v "$1" >/dev/null 2>&1; }

missing=()
for t in curl git openssl bc docker k3d kubectl helm cosign act trivy; do
  has "$t" || missing+=("$t")
done
if [ ${#missing[@]} -eq 0 ] && docker info >/dev/null 2>&1; then
  log "prerequisites: all present"
  exit 0
fi

[ "$(uname -s)" = "Linux" ] \
  || fail "missing: ${missing[*]:-docker daemon}. Automatic install is Linux only; see README.md"

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"
BIN=/usr/local/bin

case "$(uname -m)" in
  x86_64)        ARCH=amd64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) fail "unsupported architecture: $(uname -m)" ;;
esac

[ ${#missing[@]} -eq 0 ] || log "installing: ${missing[*]}"

if ! has curl || ! has git || ! has openssl || ! has bc; then
  has apt-get || fail "apt-get not found; install curl, git, openssl and bc with your package manager"
  $SUDO apt-get update -qq
  $SUDO apt-get install -y -qq curl git openssl bc ca-certificates >/dev/null
fi

if ! has docker; then
  log "docker"
  curl -fsSL https://get.docker.com | $SUDO sh >/dev/null
  [ -z "$SUDO" ] || $SUDO usermod -aG docker "$USER"
fi
if ! docker info >/dev/null 2>&1; then
  $SUDO systemctl enable --now docker >/dev/null 2>&1 || true
  docker info >/dev/null 2>&1 || [ -n "$SUDO" ] \
    || fail "docker daemon is not running"
fi

if ! has k3d; then
  log "k3d"
  curl -fsSL https://raw.githubusercontent.com/k3d-io/k3d/main/install.sh | $SUDO env K3D_INSTALL_DIR=$BIN bash >/dev/null
fi

if ! has kubectl; then
  log "kubectl"
  v=$(curl -fsSL https://dl.k8s.io/release/stable.txt)
  curl -fsSLo /tmp/kubectl "https://dl.k8s.io/release/$v/bin/linux/$ARCH/kubectl"
  $SUDO install -m 0755 /tmp/kubectl $BIN/kubectl && rm -f /tmp/kubectl
fi

if ! has helm; then
  log "helm"
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | $SUDO env HELM_INSTALL_DIR=$BIN bash >/dev/null
fi

if ! has cosign; then
  log "cosign"
  curl -fsSLo /tmp/cosign "https://github.com/sigstore/cosign/releases/latest/download/cosign-linux-$ARCH"
  $SUDO install -m 0755 /tmp/cosign $BIN/cosign && rm -f /tmp/cosign
fi

if ! has act; then
  log "act"
  curl -fsSL https://raw.githubusercontent.com/nektos/act/master/install.sh | $SUDO bash -s -- -b $BIN >/dev/null
fi

if ! has trivy; then
  log "trivy"
  curl -fsSL https://raw.githubusercontent.com/aquasecurity/trivy/main/contrib/install.sh | $SUDO sh -s -- -b $BIN >/dev/null
fi

for t in curl git openssl bc docker k3d kubectl helm cosign act trivy; do
  has "$t" || fail "still missing after install: $t"
done
if ! docker info >/dev/null 2>&1; then
  fail "docker is installed but not usable by $USER yet: log out and back in (docker group), then re-run"
fi
log "prerequisites: all present"
