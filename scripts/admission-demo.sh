#!/usr/bin/env bash
set -euo pipefail
NS=settle
CURRENT=$(kubectl -n $NS get deploy settle-worker -o jsonpath='{.spec.template.spec.containers[0].image}')
DIGEST=${CURRENT#*@}

try() {
  local label="$1" image="$2" out
  printf '\n\033[1m%s\033[0m\n  image: %s\n' "$label" "$image"
  if out=$(kubectl -n $NS set image deploy/settle-worker "worker=$image" --dry-run=server 2>&1); then
    echo "  => ADMITTED"
  else
    echo "  => REJECTED: $(echo "$out" | grep -o "failed to verify.*\|missing digest.*" | head -1)"
  fi
}

# an image the pipeline never signed: same code, one extra label, new digest
printf 'FROM localhost:5001/settle-api@%s\nLABEL built-by=someone-laptop\n' "$DIGEST" \
  | docker build -q -t localhost:5001/settle-api:unsigned-demo - >/dev/null
docker push -q localhost:5001/settle-api:unsigned-demo >/dev/null
UNSIGNED=$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' localhost:5001/settle-api:unsigned-demo \
  | grep '^localhost:5001' | head -1 | cut -d@ -f2)

try "1. running release, signed by the pipeline" "$CURRENT"
try "2. unsigned image (built outside the pipeline)" "settle-registry:5000/settle-api@$UNSIGNED"
try "3. tag instead of digest" "settle-registry:5000/settle-api:1.9.0"
echo
echo "Policy: deploy/policy/verify-image-signature.yaml (Kyverno, failurePolicy Fail)"
