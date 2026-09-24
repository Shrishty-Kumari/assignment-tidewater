#!/usr/bin/env bash
# Sign an image (by digest) and attach its SBOM as a signed attestation.
# Called only after the vulnerability and secret scans passed, so a valid
# signature means "built, tested and scanned by this pipeline". The cluster
# admits only signed settle-api images (deploy/policy/verify-image-signature.yaml).
#
#   COSIGN_KEY_B64=... COSIGN_PASSWORD=... scripts/ci/sign.sh <registry/settle-api@sha256:...> [sbom.json]
#
# Locally the key is a cosign key pair created by `make up`; in AWS it is a
# KMS asymmetric key (--key awskms:///alias/settle-<env>-cosign) or keyless
# signing with the GitHub OIDC identity.
source "$(dirname "$0")/env.sh"

REF="${1:?image ref with digest}"
SBOM="${2:-}"
[[ "$REF" == *@sha256:* ]] || fail "sign by digest only, got: $REF"
: "${COSIGN_KEY_B64:?COSIGN_KEY_B64 not set}"
: "${COSIGN_PASSWORD:?COSIGN_PASSWORD not set}"

KEY="$(mktemp)"
trap 'rm -f "$KEY"' EXIT
chmod 600 "$KEY"
printf '%s' "$COSIGN_KEY_B64" | base64 -d > "$KEY"

# no public transparency log for a laptop registry (see the policy's rekor/ctlog settings)
FLAGS=(--yes --key "$KEY" --tlog-upload=false --new-bundle-format=false --use-signing-config=false)

log "signing $REF"
cosign sign "${FLAGS[@]}" "$REF" 2>&1 | grep -v -i "deprecated" || true
if [ -n "$SBOM" ] && [ -f "$SBOM" ]; then
  log "attaching SBOM ($SBOM) as a signed CycloneDX attestation"
  cosign attest "${FLAGS[@]}" --type cyclonedx --predicate "$SBOM" "$REF" 2>&1 | grep -v -i "deprecated" || true
fi

# prove it: the signature must verify with the public half before we deploy
printf '%s' "$COSIGN_KEY_B64" | base64 -d > "$KEY"
COSIGN_PASSWORD="$COSIGN_PASSWORD" cosign public-key --key "$KEY" > "$KEY.pub"
cosign verify --key "$KEY.pub" --insecure-ignore-tlog=true "$REF" >/dev/null 2>&1 \
  || { rm -f "$KEY.pub"; fail "signature did not verify"; }
rm -f "$KEY.pub"
log "signed and verified: $REF"
