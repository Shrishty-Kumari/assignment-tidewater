variable "environment" {
  description = "Environment name (staging, prod). Used in names and tags."
  type        = string
  validation {
    condition     = contains(["staging", "prod"], var.environment)
    error_message = "environment must be staging or prod."
  }
}

variable "vpc_cidr" {
  description = "VPC CIDR. /16 per environment, non-overlapping so the VPCs can be peered later."
  type        = string
}

variable "azs" {
  description = "Exactly two availability zones."
  type        = list(string)
  validation {
    condition     = length(var.azs) == 2
    error_message = "settle is designed for two AZs."
  }
}

variable "single_nat_gateway" {
  description = "One NAT gateway for both AZs (cheaper; an AZ outage takes egress down). false = one per AZ."
  type        = bool
  default     = false
}

variable "log_retention_days" {
  description = "CloudWatch retention for flow logs, EKS control plane logs and RDS logs."
  type        = number
  default     = 365
}

# --- EKS ------------------------------------------------------------------------
variable "kubernetes_version" {
  description = "EKS Kubernetes version."
  type        = string
  default     = "1.34"
}

variable "node_instance_types" {
  description = "Instance types for the managed node group (Graviton)."
  type        = list(string)
  default     = ["m7g.large"]
}

variable "node_capacity_type" {
  description = "ON_DEMAND or SPOT."
  type        = string
  default     = "ON_DEMAND"
}

variable "node_min_size" {
  description = "Minimum nodes in the managed node group."
  type        = number
  default     = 3
}

variable "node_desired_size" {
  description = "Desired nodes (ignored after creation; the cluster autoscaler owns it)."
  type        = number
  default     = 3
}

variable "node_max_size" {
  description = "Maximum nodes in the managed node group."
  type        = number
  default     = 6
}

variable "node_disk_gb" {
  description = "Root volume size per node."
  type        = number
  default     = 50
}

variable "cluster_admin_role_arns" {
  description = "IAM roles (e.g. SSO platform-admin) granted cluster admin via EKS access entries."
  type        = list(string)
  default     = []
}

variable "github_repository" {
  description = "owner/repo allowed to deploy through GitHub OIDC."
  type        = string
}

# --- data stores ---------------------------------------------------------------
variable "db_instance_class" {
  description = "RDS instance class."
  type        = string
  default     = "db.m7g.large"
}

variable "db_allocated_storage_gb" {
  description = "Initial RDS storage (gp3); storage autoscaling up to db_max_allocated_storage_gb."
  type        = number
  default     = 100
}

variable "db_max_allocated_storage_gb" {
  description = "Upper bound for RDS storage autoscaling."
  type        = number
  default     = 500
}

variable "db_multi_az" {
  description = "Multi-AZ standby for RDS."
  type        = bool
  default     = true
}

variable "db_backup_retention_days" {
  description = "Automated backup / PITR retention."
  type        = number
  default     = 35
}

variable "db_max_connections" {
  description = "Pinned max_connections: an input of the connection budget (docs/CHANGES.md)."
  type        = number
  default     = 100
}

variable "deletion_protection" {
  description = "Protect RDS from deletion."
  type        = bool
  default     = true
}

variable "redis_node_type" {
  description = "ElastiCache node type."
  type        = string
  default     = "cache.m7g.large"
}

variable "redis_num_cache_clusters" {
  description = "1 = single node; 2+ = primary with replicas and automatic failover across AZs."
  type        = number
  default     = 2
}

variable "tags" {
  description = "Extra tags."
  type        = map(string)
  default     = {}
}
