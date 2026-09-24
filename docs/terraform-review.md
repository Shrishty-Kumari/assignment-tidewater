# Review of the previous team's Terraform draft (`infra/terraform/main.tf`, never applied)

Verdict: **not fixable in place**. The draft was rewritten as a module plus
per-environment roots (`infra/terraform/`). Every problem below is addressed
there. Severity reflects what would have happened if the draft had been
applied as written.

| # | Problem in the draft | Real-world impact if applied | Severity | Fixed by |
|---|---|---|---|---|
| 1 | Static AWS access key + secret in the `provider` block, committed to git | Anyone with repo read access (or a leaked clone) gets the key. Bots scan GitHub for `AKIA…` within minutes. The key had to be treated as compromised the moment it was committed. | Critical | No credentials in code: CI assumes a role through GitHub OIDC; humans use SSO profiles |
| 2 | RDS `publicly_accessible = true` + SG `0.0.0.0/0` on **all TCP ports** + SSH 22 open to the world | Payments database reachable from the internet, protected only by one password | Critical | RDS in isolated DB subnets (no route to the internet); SG allows 5432 only from the EKS cluster SG |
| 3 | DB password `Tidewater-2024!` hard-coded (same value as in the k8s Secret and Dockerfile), and exported by `output "db_password"` in plain text | Password in git, in state and printed by every `terraform apply` / CI log | Critical | Master password managed by RDS in Secrets Manager (`manage_master_user_password`). App credentials are generated as ephemeral values and written with `secret_string_wo`, so they never enter state. No secret outputs. |
| 4 | IAM role with `Action = "*", Resource = "*"`, shared by the EKS control plane **and** the nodes (trust: eks + ec2) | Any pod on any node could use the node credentials (IMDS) to do anything in the account | Critical | Separate cluster and node roles with only AWS-managed EKS policies. IMDSv2 with hop limit 1 (pods can't reach node credentials). Workload access via EKS Pod Identity, scoped to `settle/<env>/*` secrets. |
| 5 | S3 bucket `acl = "public-read"` for finance reports | Merchant settlement reports public on the internet (data breach, GDPR) | Critical | Bucket removed. Reports stay behind the API. If they come back: private, KMS, public access block |
| 6 | Backend `s3` with **no locking**, no encryption, and the bucket not managed anywhere | Two concurrent applies corrupt state; state holds secrets unencrypted | High | S3 backend with `use_lockfile = true` (native S3 locking), `encrypt = true` + KMS. State bucket defined in `bootstrap/` (versioned, public access blocked, TLS-only) |
| 7 | Staging and prod as **copy-pasted resources in one state** (`aws_db_instance.staging` / `.prod`) | One `apply` can change prod while testing staging. Drift between the copies. Blast radius = everything. | High | One module, instantiated by `envs/staging` and `envs/prod` with separate state files and variables |
| 8 | Single AZ, single public subnet, EKS nodes and RDS in a **public** subnet with `map_public_ip_on_launch` | No AZ redundancy (EKS itself requires 2 AZs, so `apply` would fail). Nodes get public IPs. | High | VPC across 2 AZs: public (NLB, NAT), private (nodes/pods), isolated database subnets |
| 9 | EKS API endpoint public to `0.0.0.0/0` | Kubernetes API brute-forceable from the internet | High | Private endpoint only. CI deploys from a runner inside the VPC (ADR-004) |
| 10 | No encryption: RDS `storage_encrypted = false`, ElastiCache without at-rest/in-transit encryption or AUTH, no EKS secrets encryption | Snapshots and disks readable if leaked; Redis traffic (payout jobs) in clear; any VPC client can talk to Redis | High | Customer-managed KMS key per environment for EKS secrets, RDS, ElastiCache, Secrets Manager, logs. Redis TLS + AUTH token. RDS `rds.force_ssl=1` |
| 11 | `skip_final_snapshot = true`, no backup retention, no deletion protection | `terraform destroy` or a replace (e.g. changing `identifier`) deletes the payments DB with no copy | High | `deletion_protection`, final snapshot, backups 7 d (staging) / 35 d (prod), PITR |
| 12 | `db.r5.2xlarge` + 500 GB in **staging**, `cache.r5.large`, 3× `m5.2xlarge` | ~USD 2,400/month for staging alone vs the USD 250 budget | Medium | Right-sized per environment (`infra/terraform/COSTS.md`): staging ≈ USD 225/month |
| 13 | No `required_providers` / version pins; `acl` argument removed in AWS provider v4+ | Non-reproducible plans; `validate` fails on current providers | Medium | Terraform `>= 1.11`, AWS `~> 6.0`, random `~> 3.7`, pinned in every root |
| 14 | No tags | No cost allocation per environment, no ownership | Low | `default_tags` (Service, Environment, Owner, CostCenter, ManagedBy) |
| 15 | No logs: EKS control-plane logs off, no VPC flow logs, no RDS log exports | No audit trail after an incident (we already had one without logs) | Medium | EKS audit/api/authenticator logs, VPC flow logs, RDS `postgresql` logs to CloudWatch (KMS, retention per env) |
| 16 | RDS parameters default: `max_connections` depends on instance memory | The connection budget (docs/CHANGES.md) silently changes with instance size | Medium | Parameter group pins `max_connections = 100` (the budget's input), plus `log_lock_waits`, `log_min_duration_statement`, `idle_in_transaction_session_timeout` |
| 17 | ECR not defined; images pushed as mutable `:latest` to a registry outside IaC | The 14 Aug rollback re-pulled the bad `:latest` | Medium | ECR repository with `IMMUTABLE` tags, scan on push, KMS, lifecycle policy |

## New layout

```
infra/terraform/
  bootstrap/        remote state bucket (S3 + KMS, versioned, TLS-only), applied once per account
  modules/settle/   VPC (2 AZs: public / private / isolated DB subnets), EKS, RDS, ElastiCache,
                    security groups, IAM (Pod Identity, GitHub OIDC deployer), Secrets Manager, ECR, KMS
  envs/staging/     module instance + S3 backend with native locking (use_lockfile)
  envs/prod/        same module, separate state (test-sized during the testing phase)
  COSTS.md          staging ≈ USD 223/month and the trade-offs
```

## Checks

`make tf-check` runs `terraform fmt -check`, `terraform validate` (staging,
prod, bootstrap), `tflint` (AWS ruleset) and `checkov`. All are clean:
checkov reports 0 failed checks. `trivy config` also reports 0 HIGH/CRITICAL.

## Plan against LocalStack (bonus)

```
make tf-plan-localstack    # = localstack-plan.sh staging && localstack-plan.sh prod
```

This copies the tree to a temp dir and adds override files there, so
nothing committed changes. The AWS provider points at LocalStack 4 and the
S3 backend is replaced with local state. Latest result:

| Environment | Plan |
|---|---|
| staging | **80 to add, 0 to change, 0 to destroy** |
| prod | **80 to add, 0 to change, 0 to destroy** (test-sized for now; at production size: 82, with a second NAT gateway + EIP) |

The only difference between the environments is what their variables say,
which is the point of the shared module. A plan exercises the provider
schema, the data sources (caller identity, region, partition) and every
expression. It doesn't prove that `apply` would succeed on real AWS (service
quotas, AMI availability, EKS version support). LocalStack community doesn't
emulate EKS, RDS or ElastiCache, so no apply was attempted.

## Suppressed findings

Every suppression is inline (`# checkov:skip=<id>:<reason>`) next to the resource.

| Check | Resource | Why it's suppressed |
|---|---|---|
| CKV_AWS_157 (RDS Multi-AZ) | `aws_db_instance.this` | Multi-AZ is `var.db_multi_az`: **true in prod**, false in staging to fit the USD 250 budget (COSTS.md). |
| CKV2_AWS_57 (Secrets Manager rotation) | `db_app`, `db_owner`, `redis` secrets | Rotation = bump `secret_string_wo_version` + db-bootstrap Job (DB roles), and ElastiCache `auth_token_update_strategy` (Redis). An automated rotation Lambda, or better IAM DB auth, is on `docs/NOT-DONE.md`. The RDS master secret *is* rotated by RDS. |
| CKV_AWS_109 / CKV_AWS_111 / CKV_AWS_356 | KMS key policy document | In a key policy, `Resource: "*"` means *this key*. The statement delegates to IAM in the same account (standard AWS pattern). |
| CKV_AWS_7 (KMS rotation) | `aws_kms_key.cosign` | Asymmetric (SIGN_VERIFY) keys can't auto-rotate. Rotate by creating a new key, adding it to the admission policy, then retiring the old one. |
| CKV_AWS_144, CKV2_AWS_62, CKV_AWS_18 | state bucket (bootstrap) | No cross-region replication (versioning + AWS Backup cover recovery). No event consumers. Access is audited by the org CloudTrail data-events trail, not S3 server access logs. |

## Accepted risk

* **Redis AUTH token in state.** `aws_elasticache_replication_group.auth_token`
  has no write-only variant. State is KMS-encrypted in a private, TLS-only
  bucket that only the Terraform role can read. The alternative is IAM
  authentication for ElastiCache users (NOT-DONE).


