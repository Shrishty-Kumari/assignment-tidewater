terraform {
  backend "s3" {
    bucket       = "paylane-tfstate-eu-west-1"
    key          = "settle/staging/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    kms_key_id   = "alias/paylane-tfstate"
    use_lockfile = true # native S3 state locking (Terraform >= 1.10), no DynamoDB table
  }
}

provider "aws" {
  region = "eu-west-1"
  # credentials: SSO profile for humans, GitHub OIDC role in CI - never keys in code
  default_tags {
    tags = {
      Service     = "settle"
      Environment = "staging"
      Owner       = "platform-team"
      CostCenter  = "payments"
      ManagedBy   = "terraform"
    }
  }
}

module "settle" {
  source = "../../modules/settle"

  environment        = "staging"
  vpc_cidr           = "10.20.0.0/16"
  azs                = ["eu-west-1a", "eu-west-1b"]
  single_nat_gateway = true # saves ~USD 35/month; an AZ outage takes staging egress down
  log_retention_days = 7

  # t4g.medium is the smallest size that fits the stack without CNI tuning
  # (t4g.small allows only 11 pods/node); 2 nodes, no scale-out while testing
  node_instance_types = ["t4g.medium"]
  node_capacity_type  = "ON_DEMAND"
  node_min_size       = 2
  node_desired_size   = 2
  node_max_size       = 2
  node_disk_gb        = 20

  # db.t4g.micro and cache.t4g.micro are the smallest Graviton classes
  db_instance_class           = "db.t4g.micro"
  db_allocated_storage_gb     = 20
  db_max_allocated_storage_gb = 0 # storage autoscaling off
  db_multi_az                 = false
  db_backup_retention_days    = 1
  deletion_protection         = false # test stacks are torn down; a final snapshot is still taken

  redis_node_type          = "cache.t4g.micro"
  redis_num_cache_clusters = 1

  github_repository       = "sanojkumartw/tidewater-settle"
  cluster_admin_role_arns = []
}

output "settle" {
  value = module.settle
}
