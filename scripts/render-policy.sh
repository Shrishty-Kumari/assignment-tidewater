#!/usr/bin/env bash
set -euo pipefail
KEY_FILE="${1:?public key file}"
python3 - "$KEY_FILE" "$(dirname "$0")/../deploy/policy/verify-image-signature.yaml" <<'PY'
import sys
key = open(sys.argv[1]).read().strip().splitlines()
src = open(sys.argv[2]).read().splitlines()
out = []
for line in src:
    if line.strip() == "__COSIGN_PUBLIC_KEY__":
        indent = line[: len(line) - len(line.lstrip())]
        out.extend(indent + k for k in key)
    else:
        out.append(line)
print("\n".join(out))
PY
