#!/usr/bin/env bash
# Build the image ONCE per version and publish it under an immutable tag.
# If the version already exists in the registry, the existing artifact is
# reused (promoted), never rebuilt or overwritten.
#
#   scripts/ci/build.sh <version> <git-sha>
# outputs: image_digest=sha256:..., image_ref=<pull-registry>/settle-api@sha256:...
source "$(dirname "$0")/env.sh"

VERSION="${1:?version}"
GIT_SHA="${2:-unknown}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$ ]] || fail "not a semver version: $VERSION"
TAG="$REGISTRY_PUSH/$IMAGE_NAME:$VERSION"

digest_of() { docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$1" \
  | grep "^$REGISTRY_PUSH/$IMAGE_NAME@" | head -1 | cut -d@ -f2; }

if docker pull -q "$TAG" >/dev/null 2>&1; then
  warn "$TAG already published: promoting the existing artifact, not rebuilding"
else
  log "building $TAG (git $GIT_SHA)"
  docker build -f app/Dockerfile \
    --build-arg VERSION="$VERSION" --build-arg GIT_SHA="$GIT_SHA" \
    --label org.opencontainers.image.created="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    -t "$TAG" .
  docker push -q "$TAG" >/dev/null
fi

DIGEST="$(digest_of "$TAG")"
[ -n "$DIGEST" ] || fail "could not resolve digest for $TAG"
log "artifact: $IMAGE_NAME@$DIGEST"
output image_digest "$DIGEST"
output image_ref "$REGISTRY_PULL/$IMAGE_NAME@$DIGEST"
output scan_ref "$REGISTRY_PUSH/$IMAGE_NAME@$DIGEST"
