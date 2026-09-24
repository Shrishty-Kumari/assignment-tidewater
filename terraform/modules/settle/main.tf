data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  name       = "settle-${var.environment}"
  account_id = data.aws_caller_identity.current.account_id
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.region
  tags       = merge(var.tags, { Service = "settle", Environment = var.environment })
}

# One customer-managed key per environment: EKS secrets, RDS, ElastiCache,
# Secrets Manager, CloudWatch Logs. Rotated yearly.
resource "aws_kms_key" "this" {
  description             = "${local.name} data encryption"
  deletion_window_in_days = 30
  enable_key_rotation     = true
  policy                  = data.aws_iam_policy_document.kms.json
  tags                    = local.tags
}

resource "aws_kms_alias" "this" {
  name          = "alias/${local.name}"
  target_key_id = aws_kms_key.this.key_id
}

data "aws_iam_policy_document" "kms" {
  # checkov:skip=CKV_AWS_109:Key policy: "*" in a key policy means "this key"; access is delegated to IAM of this account
  # checkov:skip=CKV_AWS_111:Key policy: same as above, write actions are limited to principals of this account
  # checkov:skip=CKV_AWS_356:Key policy: resource "*" is the only valid value in a KMS key policy
  statement {
    sid       = "AccountAdministration"
    actions   = ["kms:*"]
    resources = ["*"]
    principals {
      type        = "AWS"
      identifiers = ["arn:${local.partition}:iam::${local.account_id}:root"]
    }
  }

  statement {
    sid       = "CloudWatchLogs"
    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
    resources = ["*"]
    principals {
      type        = "Service"
      identifiers = ["logs.${local.region}.amazonaws.com"]
    }
    condition {
      test     = "ArnLike"
      variable = "kms:EncryptionContext:aws:logs:arn"
      values   = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:*"]
    }
  }
}
