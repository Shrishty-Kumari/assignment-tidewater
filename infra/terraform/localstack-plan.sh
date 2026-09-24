#!/usr/bin/env bash
# terraform plan of an environment against LocalStack (bonus in the brief).
# Nothing is applied. The committed code is not modified: the tree is copied
# to a temp dir and two Terraform override files point the AWS provider at
# LocalStack and replace the S3 backend with local state.
#
#   infra/terraform/localstack-plan.sh [staging|prod]      (make tf-plan-localstack)
set -euo pipefail
ENV="${1:-staging}"
cd "$(dirname "$0")"
ENDPOINT="${LOCALSTACK_ENDPOINT:-http://localhost:4566}"

if ! curl -sf "$ENDPOINT/_localstack/health" >/dev/null; then
  echo "starting LocalStack on :4566"
  docker rm -f settle-localstack >/dev/null 2>&1 || true
  docker run -d --name settle-localstack -p 4566:4566 localstack/localstack:4 >/dev/null
  until curl -sf "$ENDPOINT/_localstack/health" >/dev/null; do sleep 2; done
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
cp -R modules envs "$WORK/"
cd "$WORK/envs/$ENV"

cat > localstack_override.tf <<EOF
terraform {
  backend "local" {}
}

provider "aws" {
  # LocalStack accepts any credentials; these are its documented dummies
  access_key                  = "test"
  secret_key                  = "test"
  skip_credentials_validation = true
  skip_metadata_api_check     = true
  s3_use_path_style           = true
  endpoints {
    ec2            = "$ENDPOINT"
    ecr            = "$ENDPOINT"
    eks            = "$ENDPOINT"
    elasticache    = "$ENDPOINT"
    iam            = "$ENDPOINT"
    kms            = "$ENDPOINT"
    logs           = "$ENDPOINT"
    rds            = "$ENDPOINT"
    s3             = "$ENDPOINT"
    secretsmanager = "$ENDPOINT"
    sts            = "$ENDPOINT"
  }
}
EOF

terraform init -input=false -no-color >/dev/null
terraform plan -input=false -no-color -out=tfplan > plan.txt
grep -E "^Plan:|will be created" plan.txt | sed 's/^  # /  + /' | sort | uniq -c | sort -rn | head -60
terraform show -json tfplan | python3 -c '
import json, sys
p = json.load(sys.stdin)
acts = [c["change"]["actions"] for c in p.get("resource_changes", [])]
changes = [a for a in acts if a not in (["no-op"], ["read"])]
print("resource changes:", len(changes), "| create:", sum(a == ["create"] for a in changes),
      "| update/replace/delete:", sum(a != ["create"] for a in changes))
'
grep "^Plan:" plan.txt
