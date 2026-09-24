# Shared settings for the delivery scripts. Sourced, not executed.
# Under act the job runs in a container: the Docker daemon still sees the
# registry as localhost:5001, but the cluster API and ingress are reached via
# host.docker.internal. Defaults below are for running on the host.
set -euo pipefail

export NAMESPACE="${NAMESPACE:-settle}"
export IMAGE_NAME="${IMAGE_NAME:-settle-api}"
# registry as seen by the Docker daemon (push/pull)
export REGISTRY_PUSH="${REGISTRY_PUSH:-localhost:5001}"
# the same registry as seen from inside the cluster
export REGISTRY_PULL="${REGISTRY_PULL:-settle-registry:5000}"
export INGRESS_URL="${INGRESS_URL:-http://settle.localtest.me:8088}"
export INGRESS_HOST="${INGRESS_HOST:-settle.localtest.me}"
export RELEASE_DIR="${RELEASE_DIR:-.release}"
# needs PyYAML (check_db_budget.py); make passes the repo venv, CI pip-installs it
export PYTHON="${PYTHON:-python3}"
export OVERLAY="${OVERLAY:-deploy/k8s/overlays/local}"
# settlement run starts 14:00 UTC; no deploys from 13:45 to 14:45 (RCA CF1)
export FREEZE_START="${FREEZE_START:-13:45}"
export FREEZE_END="${FREEZE_END:-14:45}"

mkdir -p "$RELEASE_DIR"

log()  { printf '\033[1;34m[%s] %s\033[0m\n' "$(date -u +%H:%M:%S)" "$*"; }
warn() { printf '\033[1;33m[%s] %s\033[0m\n' "$(date -u +%H:%M:%S)" "$*"; }
fail() { printf '\033[1;31m[%s] %s\033[0m\n' "$(date -u +%H:%M:%S)" "$*" >&2; exit 1; }

# write key=value to the GitHub Actions step output when running in Actions
output() { [ -n "${GITHUB_OUTPUT:-}" ] && echo "$1=$2" >> "$GITHUB_OUTPUT"; echo "$1=$2" >> "$RELEASE_DIR/outputs"; }
