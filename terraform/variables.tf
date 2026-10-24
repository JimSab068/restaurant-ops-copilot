variable "aws_region" {
  type    = string
  default = "us-east-1"
}

variable "project_name" {
  type    = string
  default = "ops-copilot"
}

variable "environment" {
  type        = string
  description = "dev | staging | prod"
}

variable "vpc_cidr" {
  type    = string
  default = "10.20.0.0/16"
}

variable "availability_zones" {
  type    = list(string)
  default = ["us-east-1a", "us-east-1b"]
}

variable "db_name" {
  type    = string
  default = "ops_copilot"
}

variable "db_instance_class" {
  type    = string
  default = "db.t3.micro"
}

variable "db_allocated_storage_gb" {
  type    = number
  default = 50
}

# Master credentials are used only by the one-shot tenant seeder. The
# rls_admin URL is supplied separately through
# rls_admin_database_url_secret_arn.
variable "db_master_username" {
  type    = string
  default = "postgres"
}

variable "rls_admin_database_url_secret_arn" {
  type        = string
  description = "ARN of the pre-provisioned Secrets Manager secret containing the non-superuser rls_admin database URL."
}

variable "api_image" {
  type        = string
  description = "ECR image URI:tag for the API/worker shared image (immutable commit SHA tag)"
}

variable "api_desired_count" {
  type    = number
  default = 2
}

variable "worker_min_count" {
  type    = number
  default = 2
}

variable "worker_max_count" {
  type    = number
  default = 20
}

variable "bedrock_region" {
  type    = string
  default = "us-east-1"
}

variable "synthetic_execution_only" {
  type    = bool
  default = true
}

variable "acm_certificate_arn" {
  type        = string
  default     = null
  description = <<-EOT
    ACM certificate ARN for the ALB's HTTPS listener (must be issued in
    var.aws_region). When null, the ALB serves plain HTTP on port 80 only
    -- an explicit, isolated-test configuration (see alb.tf), never a
    fallback you should carry into a real staging/prod deploy. Set this
    before pointing a real domain or real tenant traffic at the ALB.
  EOT
}

variable "allowed_client_cidr" {
  type        = string
  description = "Public IPv4 CIDR allowed to reach the ALB. For one client, pass your current public IP with /32, e.g. 203.0.113.10/32."

  validation {
    condition     = can(cidrnetmask(var.allowed_client_cidr))
    error_message = "allowed_client_cidr must be a valid IPv4 CIDR."
  }
}
