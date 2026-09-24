# EKS (ADR-001). Private API endpoint only; the deploy runner lives in the VPC.
# Kubernetes secrets are envelope-encrypted with the environment's KMS key.

resource "aws_cloudwatch_log_group" "eks" {
  # EKS writes to exactly this name; created here to control retention + KMS
  name              = "/aws/eks/${local.name}/cluster"
  retention_in_days = var.log_retention_days
  kms_key_id        = aws_kms_key.this.arn
  tags              = local.tags
}

resource "aws_eks_cluster" "this" {
  name     = local.name
  version  = var.kubernetes_version
  role_arn = aws_iam_role.cluster.arn

  vpc_config {
    subnet_ids              = aws_subnet.private[*].id
    endpoint_private_access = true
    endpoint_public_access  = false
  }

  access_config {
    authentication_mode                         = "API"
    bootstrap_cluster_creator_admin_permissions = false
  }

  encryption_config {
    resources = ["secrets"]
    provider {
      key_arn = aws_kms_key.this.arn
    }
  }

  enabled_cluster_log_types = ["api", "audit", "authenticator", "controllerManager", "scheduler"]

  tags       = local.tags
  depends_on = [aws_cloudwatch_log_group.eks, aws_iam_role_policy_attachment.cluster]
}

resource "aws_iam_role" "cluster" {
  name = "${local.name}-eks-cluster"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "eks.amazonaws.com" }
      Action    = ["sts:AssumeRole", "sts:TagSession"]
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "cluster" {
  role       = aws_iam_role.cluster.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/AmazonEKSClusterPolicy"
}

# --- nodes -----------------------------------------------------------------------
resource "aws_iam_role" "node" {
  name = "${local.name}-eks-node"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "node" {
  for_each = toset([
    "AmazonEKSWorkerNodePolicy",
    "AmazonEKS_CNI_Policy",
    "AmazonEC2ContainerRegistryPullOnly",
  ])
  role       = aws_iam_role.node.name
  policy_arn = "arn:${local.partition}:iam::aws:policy/${each.value}"
}

resource "aws_launch_template" "node" {
  name_prefix = "${local.name}-node-"

  # IMDSv2 only, hop limit 1: pods cannot reach the node's instance
  # credentials (the draft's node role had "*:*")
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size           = var.node_disk_gb
      volume_type           = "gp3"
      encrypted             = true
      kms_key_id            = aws_kms_key.this.arn
      delete_on_termination = true
    }
  }

  tag_specifications {
    resource_type = "instance"
    tags          = merge(local.tags, { Name = "${local.name}-node" })
  }
  tags = local.tags
}

resource "aws_eks_node_group" "default" {
  cluster_name    = aws_eks_cluster.this.name
  node_group_name = "default"
  node_role_arn   = aws_iam_role.node.arn
  subnet_ids      = aws_subnet.private[*].id
  ami_type        = "AL2023_ARM_64_STANDARD"
  instance_types  = var.node_instance_types
  capacity_type   = var.node_capacity_type

  launch_template {
    id      = aws_launch_template.node.id
    version = aws_launch_template.node.latest_version
  }

  scaling_config {
    min_size     = var.node_min_size
    desired_size = var.node_desired_size
    max_size     = var.node_max_size
  }

  update_config {
    max_unavailable = 1
  }

  labels = { workload = "general" }
  tags   = local.tags

  lifecycle {
    ignore_changes = [scaling_config[0].desired_size]
  }

  depends_on = [aws_iam_role_policy_attachment.node]
}

# --- add-ons -----------------------------------------------------------------------
resource "aws_eks_addon" "this" {
  for_each = {
    "vpc-cni"                = jsonencode({ enableNetworkPolicy = "true" }) # enforces deploy/k8s NetworkPolicies
    "coredns"                = null
    "kube-proxy"             = null
    "eks-pod-identity-agent" = null
  }
  cluster_name                = aws_eks_cluster.this.name
  addon_name                  = each.key
  configuration_values        = each.value
  resolve_conflicts_on_update = "OVERWRITE"
  tags                        = local.tags
  depends_on                  = [aws_eks_node_group.default]
}

# --- access ------------------------------------------------------------------------
resource "aws_eks_access_entry" "admins" {
  for_each      = toset(var.cluster_admin_role_arns)
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
  tags          = local.tags
}

resource "aws_eks_access_policy_association" "admins" {
  for_each      = toset(var.cluster_admin_role_arns)
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = each.value
  policy_arn    = "arn:${local.partition}:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy"
  access_scope {
    type = "cluster"
  }
  depends_on = [aws_eks_access_entry.admins]
}

# the CI deploy role may only edit the settle namespace
resource "aws_eks_access_entry" "deployer" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_iam_role.deployer.arn
  tags          = local.tags
}

resource "aws_eks_access_policy_association" "deployer" {
  cluster_name  = aws_eks_cluster.this.name
  principal_arn = aws_iam_role.deployer.arn
  policy_arn    = "arn:${local.partition}:eks::aws:cluster-access-policy/AmazonEKSEditPolicy"
  access_scope {
    type       = "namespace"
    namespaces = ["settle"]
  }
  depends_on = [aws_eks_access_entry.deployer]
}
