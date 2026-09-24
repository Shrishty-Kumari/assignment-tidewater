# Workload and CI identities. settle-api and settle-worker need no AWS API
# access at all (they get credentials as env vars from a k8s Secret), so they
# have no role. Only the External Secrets Operator reads Secrets Manager.

# --- External Secrets Operator (EKS Pod Identity) -------------------------------
data "aws_iam_policy_document" "pod_identity_assume" {
  statement {
    actions = ["sts:AssumeRole", "sts:TagSession"]
    principals {
      type        = "Service"
      identifiers = ["pods.eks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "external_secrets" {
  name               = "${local.name}-external-secrets"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "external_secrets" {
  name = "read-settle-secrets"
  role = aws_iam_role.external_secrets.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "ReadSettleSecretsOnly"
        Effect = "Allow"
        Action = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
        Resource = [
          aws_secretsmanager_secret.db_app.arn,
          aws_secretsmanager_secret.db_owner.arn,
          aws_secretsmanager_secret.redis.arn,
        ]
      },
      {
        Sid      = "DecryptViaSecretsManagerOnly"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = [aws_kms_key.this.arn]
        Condition = {
          StringEquals = { "kms:ViaService" = "secretsmanager.${local.region}.amazonaws.com" }
        }
      },
    ]
  })
}

resource "aws_eks_pod_identity_association" "external_secrets" {
  cluster_name    = aws_eks_cluster.this.name
  namespace       = "external-secrets"
  service_account = "external-secrets"
  role_arn        = aws_iam_role.external_secrets.arn
  tags            = local.tags
}

# --- CI deployer (GitHub Actions OIDC, no stored AWS keys) --------------------------
# One OIDC provider per account; created here for a self-contained example.
resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  tags           = local.tags
}

data "aws_iam_policy_document" "deployer_assume" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # only workflow runs bound to this repo's environment (with its approval
    # rules) can assume the role - not any branch or fork
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:${var.environment}"]
    }
  }
}

resource "aws_iam_role" "deployer" {
  name                 = "${local.name}-deployer"
  assume_role_policy   = data.aws_iam_policy_document.deployer_assume.json
  max_session_duration = 3600
  tags                 = local.tags
}

resource "aws_iam_role_policy" "deployer" {
  name = "push-image-and-reach-cluster"
  role = aws_iam_role.deployer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = ["*"] # this action does not support resource-level permissions
      },
      {
        Sid    = "EcrPushSettleOnly"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability", "ecr:BatchGetImage", "ecr:CompleteLayerUpload",
          "ecr:DescribeImages", "ecr:GetDownloadUrlForLayer", "ecr:InitiateLayerUpload",
          "ecr:PutImage", "ecr:UploadLayerPart",
        ]
        Resource = [aws_ecr_repository.settle.arn]
      },
      {
        Sid      = "DescribeCluster"
        Effect   = "Allow"
        Action   = ["eks:DescribeCluster"]
        Resource = [aws_eks_cluster.this.arn]
      },
    ]
  })
}

# --- ECR ------------------------------------------------------------------------------
resource "aws_ecr_repository" "settle" {
  name = "settle-${var.environment}/settle-api"
  # a version tag can never be moved (14 Aug: :latest pointed at the bad image)
  image_tag_mutability = "IMMUTABLE"
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.this.arn
  }
  tags = local.tags
}

resource "aws_ecr_lifecycle_policy" "settle" {
  repository = aws_ecr_repository.settle.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "keep the last 50 images (rollback targets), expire older"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 50 }
      action       = { type = "expire" }
    }]
  })
}

# --- image signing (cosign with a KMS key) ---------------------------------------------
# The pipeline signs every scanned image with this key (cosign --key
# awskms:///alias/settle-<env>-cosign); Kyverno admits only settle-api
# digests that verify against it. The private key never leaves KMS.
resource "aws_kms_key" "cosign" {
  # checkov:skip=CKV_AWS_7:Automatic rotation is not supported for asymmetric KMS keys; rotate by creating a new key and adding it to the admission policy before retiring the old one
  description              = "${local.name} container image signing (cosign)"
  key_usage                = "SIGN_VERIFY"
  customer_master_key_spec = "ECC_NIST_P256"
  deletion_window_in_days  = 30
  policy                   = data.aws_iam_policy_document.kms.json
  tags                     = local.tags
}

resource "aws_kms_alias" "cosign" {
  name          = "alias/${local.name}-cosign"
  target_key_id = aws_kms_key.cosign.key_id
}

resource "aws_iam_role_policy" "deployer_sign" {
  name = "sign-images"
  role = aws_iam_role.deployer.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid      = "CosignSign"
      Effect   = "Allow"
      Action   = ["kms:Sign", "kms:GetPublicKey", "kms:DescribeKey"]
      Resource = [aws_kms_key.cosign.arn]
    }]
  })
}

# Kyverno's admission controller: read the public key and the signatures in ECR
resource "aws_iam_role" "kyverno" {
  name               = "${local.name}-kyverno"
  assume_role_policy = data.aws_iam_policy_document.pod_identity_assume.json
  tags               = local.tags
}

resource "aws_iam_role_policy" "kyverno" {
  name = "verify-image-signatures"
  role = aws_iam_role.kyverno.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CosignPublicKey"
        Effect   = "Allow"
        Action   = ["kms:GetPublicKey", "kms:DescribeKey"]
        Resource = [aws_kms_key.cosign.arn]
      },
      {
        Sid      = "EcrAuth"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = ["*"] # this action does not support resource-level permissions
      },
      {
        Sid      = "ReadSignatures"
        Effect   = "Allow"
        Action   = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer", "ecr:DescribeImages"]
        Resource = [aws_ecr_repository.settle.arn]
      },
    ]
  })
}

resource "aws_eks_pod_identity_association" "kyverno" {
  cluster_name    = aws_eks_cluster.this.name
  namespace       = "kyverno"
  service_account = "kyverno-admission-controller"
  role_arn        = aws_iam_role.kyverno.arn
  tags            = local.tags
}
