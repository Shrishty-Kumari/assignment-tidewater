terraform {
  backend "s3" {
    bucket       = "paylane-tfstate-eu-west-1"
    key          = "settle/prod/terraform.tfstate"
    region       = "eu-west-1"
    encrypt      = true
    kms_key_id   = "alias/paylane-tfstate"
    use_lockfile = true
  }
}

provider "aws" {
  region = "eu-west-1"
  default_tags {
    tags = {
      Service     = "settle"
      Environment = "prod"
      Owner       = "platform-team"
      CostCenter  = "payments"
      ManagedBy   = "terraform"
    }
  }
}

module "settle" {
  source = "../../modules/settle"

  environment        = "prod"
  vpc_cidr           = "10.30.0.0/16"
  azs                = ["eu-west-1a", "eu-west-1b"]
  single_nat_gateway = true # test-sized: one NAT for both AZs
  log_retention_days = 30

  node_instance_types = ["t4g.medium"]
  node_capacity_type  = "ON_DEMAND"
  node_min_size       = 2
  node_desired_size   = 2
  node_max_size       = 3
  node_disk_gb        = 20

  db_instance_class           = "db.t4g.micro"
  db_allocated_storage_gb     = 20
  db_max_allocated_storage_gb = 50
  db_multi_az                 = false # test-sized: no standby
  db_backup_retention_days    = 7
  deletion_protection         = true

  redis_node_type          = "cache.t4g.micro"
  redis_num_cache_clusters = 1 

  github_repository       = "sanojkumartw/tidewater-settle"
  cluster_admin_role_arns = []
}

output "settle" {
  value = module.settle
}
