#!/usr/bin/env bash
# Static checks for the AWS infrastructure. Nothing is applied and no AWS
# credentials are needed (backends are skipped with -backend=false).
#
#   infra/terraform/check.sh
set -euo pipefail
cd "$(dirname "$0")"
export TF_IN_AUTOMATION=1 TF_PLUGIN_CACHE_DIR="${TF_PLUGIN_CACHE_DIR:-$HOME/.terraform.d/plugin-cache}"
mkdir -p "$TF_PLUGIN_CACHE_DIR"

step() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }

step "terraform fmt -check (all)"
terraform fmt -check -recursive -diff

for root in envs/staging envs/prod bootstrap; do
  step "terraform validate: $root"
  terraform -chdir="$root" init -backend=false -input=false -no-color >/dev/null
  terraform -chdir="$root" validate -no-color
done

step "tflint (AWS ruleset)"
tflint --init >/dev/null
for root in envs/staging envs/prod bootstrap; do
  echo "-- $root"
  tflint --chdir="$root" --config="$PWD/.tflint.hcl" --format=compact
done

step "checkov (suppressions documented in README.md)"
checkov --quiet --compact --framework terraform --skip-download \
  -d modules/settle -d envs/staging -d envs/prod -d bootstrap
echo "all checks passed"
