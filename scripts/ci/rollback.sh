#!/usr/bin/env bash
# Automatic rollback: return both deployments to the image that was running
# before this release. Images are digests, so "previous" really is the
# previous artifact (on 14 Aug `rollout undo` re-pulled :latest = the bad one).
# No schema change is needed: every migration is compatible with N-1.
#
#   scripts/ci/rollback.sh <previous-image-ref> [reason]
source "$(dirname "$0")/env.sh"

PREV="${1:-}"
REASON="${2:-post-deploy verification failed}"
[ -n "$PREV" ] || fail "no previous release recorded; cannot roll back automatically (first deploy?)"

warn "ROLLING BACK: $REASON"
for d in settle-api settle-worker; do
  current=$(kubectl -n "$NAMESPACE" get deploy "$d" -o jsonpath='{.spec.template.spec.containers[0].image}')
  if [ "$current" = "$PREV" ]; then
    log "$d already on $PREV"
    continue
  fi
  kubectl -n "$NAMESPACE" rollout undo "deployment/$d" >/dev/null
  kubectl -n "$NAMESPACE" rollout status "deployment/$d" --timeout=240s
  now=$(kubectl -n "$NAMESPACE" get deploy "$d" -o jsonpath='{.spec.template.spec.containers[0].image}')
  if [ "$now" != "$PREV" ]; then
    # undo went to an unexpected revision: pin the previous digest explicitly
    kubectl -n "$NAMESPACE" set image "deployment/$d" "*=$PREV" >/dev/null
    kubectl -n "$NAMESPACE" rollout status "deployment/$d" --timeout=240s
  fi
done
PREV_VERSION=$(grep '^previous_version=' "$RELEASE_DIR/outputs" 2>/dev/null | tail -1 | cut -d= -f2 || true)
kubectl -n "$NAMESPACE" annotate deploy settle-api settle-worker --overwrite \
  "settle.paylane.io/version=${PREV_VERSION:-rolled-back}" >/dev/null
log "rolled back to ${PREV_VERSION:-previous} ($PREV)"
