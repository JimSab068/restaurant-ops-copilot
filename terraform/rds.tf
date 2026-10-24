# ---------------------------------------------------------------------------
# Phase 3 (part 2): RDS PostgreSQL in private subnets.
#
# IMPORTANT: this only provisions the DATABASE. Creating the rls_admin /
# app_user roles and running Component_5/enable_rls.py is deliberately NOT
# done here -- that's application-schema work, handled by the bootstrap ECS
# task (ecs.tf's aws_ecs_task_definition.bootstrap), which runs AFTER this
# RDS instance exists and BEFORE the api/worker services receive traffic.
# ---------------------------------------------------------------------------

resource "aws_db_subnet_group" "this" {
  name       = "${local.name_prefix}-db-subnets"
  subnet_ids = [for s in aws_subnet.private : s.id]
}

resource "random_password" "db_master" {
  length  = 32
  special = false # avoid characters that need extra escaping in connection URLs
}

resource "aws_secretsmanager_secret" "db_master" {
  name = "${local.name_prefix}/db-master-password"
}

resource "aws_secretsmanager_secret_version" "db_master" {
  secret_id     = aws_secretsmanager_secret.db_master.id
  secret_string = random_password.db_master.result
}

resource "aws_db_instance" "this" {
  identifier     = "${local.name_prefix}-db"
  engine         = "postgres"
  engine_version = "16"

  instance_class        = var.db_instance_class
  allocated_storage     = var.db_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true

  db_name  = var.db_name
  username = var.db_master_username
  password = random_password.db_master.result

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  publicly_accessible    = false

  multi_az                 = var.environment == "prod"
  backup_retention_period  = var.environment == "prod" ? 7 : 1
  deletion_protection      = var.environment == "prod"
  skip_final_snapshot      = var.environment != "prod"
  final_snapshot_identifier = var.environment == "prod" ? "${local.name_prefix}-final" : null

  tags = { Name = "${local.name_prefix}-db" }
}

# The app-level master connection string, for the bootstrap task only.
# Application traffic uses app_user, created by that same bootstrap task,
# whose credentials you issue separately (see README note in outputs.tf).
resource "aws_secretsmanager_secret" "db_master_url" {
  name = "${local.name_prefix}/db-master-url"
}

resource "aws_secretsmanager_secret_version" "db_master_url" {
  secret_id = aws_secretsmanager_secret.db_master_url.id
  secret_string = "postgresql://${var.db_master_username}:${random_password.db_master.result}@${aws_db_instance.this.address}:5432/${var.db_name}"
}
