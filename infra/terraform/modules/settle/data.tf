# RDS Postgres 15 and ElastiCache Redis 7, in the isolated database subnets,
# reachable only from the EKS cluster security group.

resource "aws_db_subnet_group" "this" {
  name       = local.name
  subnet_ids = aws_subnet.database[*].id
  tags       = local.tags
}

resource "aws_security_group" "rds" {
  name        = "${local.name}-rds"
  description = "Postgres for settle: 5432 from EKS nodes/pods only"
  vpc_id      = aws_vpc.this.id
  tags        = merge(local.tags, { Name = "${local.name}-rds" })
}

resource "aws_vpc_security_group_ingress_rule" "rds_from_eks" {
  security_group_id            = aws_security_group.rds.id
  description                  = "Postgres from the EKS cluster security group"
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_db_parameter_group" "this" {
  name        = "${local.name}-pg15"
  family      = "postgres15"
  description = "settle: pinned connection limit, TLS only, lock and slow query logging"

  # input of the connection budget; otherwise it silently follows instance memory
  parameter {
    name         = "max_connections"
    value        = tostring(var.db_max_connections)
    apply_method = "pending-reboot"
  }
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
  # the evidence that made the 14 Aug RCA possible
  parameter {
    name  = "log_lock_waits"
    value = "1"
  }
  parameter {
    name  = "log_min_duration_statement"
    value = "1000"
  }
  parameter {
    name  = "idle_in_transaction_session_timeout"
    value = "60000"
  }
  # notice dead clients even while a backend waits on a lock (RCA RC2)
  parameter {
    name  = "client_connection_check_interval"
    value = "10000"
  }
  parameter {
    name  = "log_connections"
    value = "1"
  }
  parameter {
    name  = "log_disconnections"
    value = "1"
  }
  tags = local.tags
}

resource "aws_iam_role" "rds_monitoring" {
  name = "${local.name}-rds-monitoring"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "rds_monitoring" {
  role       = aws_iam_role.rds_monitoring.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

resource "aws_db_instance" "this" {
  # checkov:skip=CKV_AWS_157:Multi-AZ is var.db_multi_az: true in prod, false in staging to meet the USD 250 budget (infra/terraform/COSTS.md)
  identifier     = local.name
  engine         = "postgres"
  engine_version = "15"
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.this.arn

  db_name  = "settle"
  username = "settle_admin"
  # master password generated, stored and rotated by RDS in Secrets Manager;
  # never in Terraform state, code or outputs
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.this.arn

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false
  multi_az               = var.db_multi_az
  parameter_group_name   = aws_db_parameter_group.this.name
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  iam_database_authentication_enabled = true
  auto_minor_version_upgrade          = true
  allow_major_version_upgrade         = false
  maintenance_window                  = "sun:03:00-sun:04:00"
  backup_window                       = "01:00-02:00"
  backup_retention_period             = var.db_backup_retention_days
  copy_tags_to_snapshot               = true
  delete_automated_backups            = false
  deletion_protection                 = var.deletion_protection
  skip_final_snapshot                 = false
  final_snapshot_identifier           = "${local.name}-final"

  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.this.arn
  performance_insights_retention_period = 7
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.rds_monitoring.arn
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]

  tags = local.tags
}

# --- Redis -------------------------------------------------------------------------
resource "aws_elasticache_subnet_group" "this" {
  name       = local.name
  subnet_ids = aws_subnet.database[*].id
  tags       = local.tags
}

resource "aws_security_group" "redis" {
  name        = "${local.name}-redis"
  description = "Redis for settle: 6379 from EKS nodes/pods only"
  vpc_id      = aws_vpc.this.id
  tags        = merge(local.tags, { Name = "${local.name}-redis" })
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_eks" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Redis from the EKS cluster security group"
  referenced_security_group_id = aws_eks_cluster.this.vpc_config[0].cluster_security_group_id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

# ElastiCache has no write-only argument for the AUTH token, so it lives in
# state; state is KMS-encrypted and readable only by the Terraform role.
resource "random_password" "redis_auth" {
  length  = 48
  special = false
}

resource "aws_cloudwatch_log_group" "redis" {
  name              = "/aws/elasticache/${local.name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.this.arn
  tags              = local.tags
}

resource "aws_elasticache_replication_group" "this" {
  replication_group_id = local.name
  description          = "settle job queue (${var.environment})"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_node_type
  port                 = 6379
  parameter_group_name = "default.redis7"

  num_cache_clusters         = var.redis_num_cache_clusters
  automatic_failover_enabled = var.redis_num_cache_clusters > 1
  multi_az_enabled           = var.redis_num_cache_clusters > 1

  subnet_group_name  = aws_elasticache_subnet_group.this.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.this.arn
  transit_encryption_enabled = true
  auth_token                 = random_password.redis_auth.result

  snapshot_retention_limit   = 3
  snapshot_window            = "02:00-03:00"
  maintenance_window         = "sun:04:00-sun:05:00"
  auto_minor_version_upgrade = true
  apply_immediately          = false

  log_delivery_configuration {
    destination      = aws_cloudwatch_log_group.redis.name
    destination_type = "cloudwatch-logs"
    log_format       = "json"
    log_type         = "slow-log"
  }

  tags = local.tags
}
