#!/usr/bin/env bash
# Deploy one immutable image (by digest) to the cluster:
#   1. freeze-window check
#   2. record what is running now (the rollback target)
#   3. run migrations as a Job and wait - never in parallel with the rollout
#   4. roll out api + worker and wait for both
#
#   scripts/ci/deploy.sh <image-ref-with-digest> <version>
# outputs: previous_image=<ref or empty>
source "$(dirname "$0")/env.sh"

IMAGE_REF="${1:?image ref (registry/settle-api@sha256:...)}"
VERSION="${2:?version}"
[[ "$IMAGE_REF" == *@sha256:* ]] || fail "refusing to deploy a mutable reference: $IMAGE_REF"
DIGEST="${IMAGE_REF#*@}"
SHORT="${DIGEST#sha256:}"; SHORT="${SHORT:0:10}"

# 1. freeze window ----------------------------------------------------------
now=$(date -u +%H:%M)
if [[ "$now" > "$FREEZE_START" && "$now" < "$FREEZE_END" && "${FREEZE_OVERRIDE:-false}" != "true" ]]; then
  fail "deploy freeze: settlement run window $FREEZE_START-$FREEZE_END UTC (set FREEZE_OVERRIDE=true with a reason)"
fi

# 2. rollback target ----------------------------------------------------------
# The last release that passed post-deploy verification (recorded by
# mark_verified.sh), not simply whatever is running: after an interrupted run
# the running release may be the unverified one.
RUNNING="$(kubectl -n "$NAMESPACE" get deploy settle-api -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
RUNNING_VERSION="$(kubectl -n "$NAMESPACE" get deploy settle-api -o jsonpath='{.metadata.annotations.settle\.paylane\.io/version}' 2>/dev/null || true)"
GOOD="$(kubectl -n "$NAMESPACE" get deploy settle-api -o jsonpath='{.metadata.annotations.settle\.paylane\.io/verified-image}' 2>/dev/null || true)"
GOOD_VERSION="$(kubectl -n "$NAMESPACE" get deploy settle-api -o jsonpath='{.metadata.annotations.settle\.paylane\.io/verified-version}' 2>/dev/null || true)"
log "currently running: ${RUNNING_VERSION:-nothing} ${RUNNING:+($RUNNING)}"
if [ -n "$GOOD" ] && [ "$GOOD" != "$IMAGE_REF" ]; then
  PREV="$GOOD"; PREV_VERSION="$GOOD_VERSION"
  [ "$GOOD" = "$RUNNING" ] || warn "running release was never verified; rollback target is the last verified one"
else
  PREV="$RUNNING"; PREV_VERSION="$RUNNING_VERSION"
fi
output previous_image "$PREV"
output previous_version "$PREV_VERSION"
log "rollback target: ${PREV_VERSION:-none} ${PREV:+($PREV)}"
if [ "$RUNNING" = "$IMAGE_REF" ]; then
  warn "this digest is already deployed; re-applying manifests only"
fi

# 3. render manifests with the digest -----------------------------------------
rm -rf "$RELEASE_DIR/overlay" && mkdir -p "$RELEASE_DIR/overlay"
cat > "$RELEASE_DIR/overlay/kustomization.yaml" <<EOF
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization
resources:
  - ../../$OVERLAY
images:
  - name: settle-api
    newName: ${IMAGE_REF%@*}
    digest: $DIGEST
commonAnnotations:
  settle.paylane.io/version: "$VERSION"
EOF
kubectl kustomize "$RELEASE_DIR/overlay" > "$RELEASE_DIR/manifests.yaml"

# connection budget is a release gate too
"$PYTHON" scripts/check_db_budget.py "$RELEASE_DIR/manifests.yaml"

# 4. migrations ----------------------------------------------------------------
# prerequisites the Job needs (namespace, service accounts, network policies);
# deployments and config are only touched by the rollout itself
"$PYTHON" - "$RELEASE_DIR/manifests.yaml" > "$RELEASE_DIR/prereqs.yaml" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(open(sys.argv[1])) if d]
print(yaml.safe_dump_all([d for d in docs if d["kind"] in ("Namespace", "ServiceAccount", "NetworkPolicy")]))
PY
kubectl apply -f "$RELEASE_DIR/prereqs.yaml" >/dev/null

JOB="settle-migrate-$SHORT"
log "migrations: job/$JOB"
kubectl -n "$NAMESPACE" delete job "$JOB" --ignore-not-found >/dev/null
sed -e "s#^  name: settle-migrate\$#  name: $JOB#" -e "s#image: settle-api\$#image: $IMAGE_REF#" \
  deploy/k8s/jobs/migrate-job.yaml | kubectl apply -f - >/dev/null
for i in $(seq 1 180); do
  s=$(kubectl -n "$NAMESPACE" get job "$JOB" -o jsonpath='{.status.succeeded}{"/"}{.status.failed}')
  [ "${s%/*}" = "1" ] && break
  if [ "$i" -eq 20 ] && [ -z "$(kubectl -n "$NAMESPACE" get pods -l job-name="$JOB" -o name)" ]; then
    kubectl -n "$NAMESPACE" get events --field-selector involvedObject.name="$JOB" | tail -5
    fail "migration job could not create a pod"
  fi
  if [ -n "${s#*/}" ] && [ "${s#*/}" -ge 2 ]; then
    kubectl -n "$NAMESPACE" logs "job/$JOB" --tail=50 || true
    fail "migration job failed; nothing was rolled out (schema changes are additive, app untouched)"
  fi
  sleep 3
done
[ "${s%/*}" = "1" ] || fail "migration job timed out"
kubectl -n "$NAMESPACE" logs "job/$JOB" --tail=20 | sed 's/^/    /'

# 5. rollout -------------------------------------------------------------------
log "rolling out $VERSION ($IMAGE_REF)"
kubectl apply -f "$RELEASE_DIR/manifests.yaml" >/dev/null
for d in settle-api settle-worker; do
  if ! kubectl -n "$NAMESPACE" rollout status "deployment/$d" --timeout=240s; then
    fail "rollout of $d did not complete"
  fi
done
log "rollout complete; handing over to post-deploy verification"
