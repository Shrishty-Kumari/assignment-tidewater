# How the workload gets its secrets (ADR-005):
#
#   Secrets Manager (KMS) ──► External Secrets Operator ──► k8s Secret ──► pod env
#        settle/<env>/*         (EKS Pod Identity role        settle-db,
#                                below, read-only on           settle-db-migrate,
#                                settle/<env>/* only)          settle-redis
#
# Values are written with write-only arguments from ephemeral passwords, so
# they are never stored in Terraform state. Bump *_version to rotate; the
# database role password is then updated by the db-bootstrap Job (runbook).

ephemeral "random_password" "db_app" {
  length  = 40
  special = false
}

ephemeral "random_password" "db_owner" {
  length  = 40
  special = false
}

resource "aws_secretsmanager_secret" "db_app" {
  # checkov:skip=CKV2_AWS_57:App/owner role passwords are rotated by bumping secret_string_wo_version + the db-bootstrap Job; a rotation Lambda is on docs/NOT-DONE.md (IAM DB auth preferred)
  name                    = "settle/${var.environment}/database-app"
  description             = "DATABASE_URL for settle-api and settle-worker (DML-only role settle_app)"
  kms_key_id              = aws_kms_key.this.arn
  recovery_window_in_days = 30
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "db_app" {
  secret_id = aws_secretsmanager_secret.db_app.id
  secret_string_wo = jsonencode({
    DATABASE_URL = "postgresql+psycopg://settle_app:${ephemeral.random_password.db_app.result}@${aws_db_instance.this.address}:5432/settle?sslmode=require"
    PASSWORD     = ephemeral.random_password.db_app.result
  })
  secret_string_wo_version = 1
}

resource "aws_secretsmanager_secret" "db_owner" {
  # checkov:skip=CKV2_AWS_57:See db_app
  name                    = "settle/${var.environment}/database-migrate"
  description             = "DATABASE_URL for the migration Job (schema owner settle_owner)"
  kms_key_id              = aws_kms_key.this.arn
  recovery_window_in_days = 30
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "db_owner" {
  secret_id = aws_secretsmanager_secret.db_owner.id
  secret_string_wo = jsonencode({
    DATABASE_URL = "postgresql+psycopg://settle_owner:${ephemeral.random_password.db_owner.result}@${aws_db_instance.this.address}:5432/settle?sslmode=require"
    PASSWORD     = ephemeral.random_password.db_owner.result
  })
  secret_string_wo_version = 1
}

resource "aws_secretsmanager_secret" "redis" {
  # checkov:skip=CKV2_AWS_57:Redis AUTH token rotation uses ElastiCache's auth_token_update_strategy (ROTATE); scripted in the runbook, not a Lambda
  name                    = "settle/${var.environment}/redis"
  description             = "REDIS_URL (TLS + AUTH) for settle-api and settle-worker"
  kms_key_id              = aws_kms_key.this.arn
  recovery_window_in_days = 30
  tags                    = local.tags
}

resource "aws_secretsmanager_secret_version" "redis" {
  secret_id = aws_secretsmanager_secret.redis.id
  secret_string_wo = jsonencode({
    REDIS_URL = "rediss://:${random_password.redis_auth.result}@${aws_elasticache_replication_group.this.primary_endpoint_address}:6379/0"
  })
  secret_string_wo_version = 1
}
