#!/usr/bin/env bash
# Record a release that passed post-deploy verification. deploy.sh uses it as
# the rollback target for the next release.
#
#   scripts/ci/mark_verified.sh <image-ref-with-digest> <version>
source "$(dirname "$0")/env.sh"

IMAGE_REF="${1:?image ref}"
VERSION="${2:?version}"
kubectl -n "$NAMESPACE" annotate deploy settle-api settle-worker --overwrite \
  "settle.paylane.io/verified-image=$IMAGE_REF" \
  "settle.paylane.io/verified-version=$VERSION" >/dev/null
log "recorded $VERSION as the last verified release ($IMAGE_REF)"
