# No secret values are output (the draft printed the prod DB password).

output "vpc_id" {
  value = aws_vpc.this.id
}

output "private_subnet_ids" {
  value = aws_subnet.private[*].id
}

output "database_subnet_cidrs" {
  description = "For NetworkPolicy egress ipBlocks in the AWS overlay."
  value       = local.database_cidrs
}

output "eks_cluster_name" {
  value = aws_eks_cluster.this.name
}

output "rds_endpoint" {
  value = aws_db_instance.this.address
}

output "rds_master_secret_arn" {
  description = "ARN (not value) of the RDS-managed master secret."
  value       = aws_db_instance.this.master_user_secret[0].secret_arn
}

output "redis_primary_endpoint" {
  value = aws_elasticache_replication_group.this.primary_endpoint_address
}

output "ecr_repository_url" {
  value = aws_ecr_repository.settle.repository_url
}

output "deployer_role_arn" {
  value = aws_iam_role.deployer.arn
}

output "secret_arns" {
  value = {
    database_app     = aws_secretsmanager_secret.db_app.arn
    database_migrate = aws_secretsmanager_secret.db_owner.arn
    redis            = aws_secretsmanager_secret.redis.arn
  }
}

output "cosign_kms_key" {
  description = "Signing key reference for cosign and the Kyverno policy."
  value       = "awskms:///${aws_kms_alias.cosign.name}"
}
