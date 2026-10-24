output "alb_dns_name" {
  value       = aws_lb.api.dns_name
  description = "Point your staging/prod domain's CNAME here, or hit this directly over HTTP for now (see alb.tf's TODO on adding HTTPS)."
}

output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}

output "sqs_queue_url" {
  value = aws_sqs_queue.work_requests.url
}

output "sqs_dlq_url" {
  value = aws_sqs_queue.dlq.url
}

output "db_endpoint" {
  value     = aws_db_instance.this.address
  sensitive = false
}

output "ecs_cluster_name" {
  value = aws_ecs_cluster.this.name
}

output "bootstrap_task_definition_arn" {
  value       = aws_ecs_task_definition.bootstrap.arn
  description = "Run this once with `aws ecs run-task` before the api/worker services can pass /readyz -- see ecs.tf's comment above the resource."
}

output "acceptance_fixtures_secret_arn" {
  value       = aws_secretsmanager_secret.acceptance_fixtures.arn
  description = "Retrieve this secret to a protected local tenant_fixtures.json file before running deployed acceptance tests."
}

output "release_gate_reader_secret_arn" {
  value       = aws_secretsmanager_secret.release_gate_reader_url.arn
  description = "Secret containing the read-only release-gate database URL; provision its role with the release-gate setup task."
}

output "release_gate_setup_task_definition_arn" {
  value       = aws_ecs_task_definition.release_gate_setup.arn
  description = "One-off task that creates or rotates the read-only release_gate_reader credential."
}

output "sql_release_gates_task_definition_arn" {
  value       = aws_ecs_task_definition.sql_release_gates.arn
  description = "One-off read-only task that runs the acceptance SQL release gates."
}

output "next_steps" {
  value = <<-EOT
    1. Terraform state is stored locally in terraform/terraform.tfstate.
       Back up and protect that file; it contains sensitive infrastructure
       metadata. Do not commit it.
    2. Set `allowed_client_cidr` to your current public IPv4 with `/32`,
       then run `terraform init`, review `terraform plan`, and apply.
    3. Set the Secrets Manager secret whose ARN is supplied as
       rls_admin_database_url_secret_arn to
       postgresql://rls_admin:<password>@<db_endpoint>:5432/<db_name>.
       Choose a password containing URL-safe characters. The bootstrap task
       creates that database role using the password in the URL.
    4. Run the bootstrap task once (bootstrap_task_definition_arn output above),
       in a private subnet with the ecs_worker security group (or a dedicated
       bootstrap SG with egress-only to RDS). It creates `rls_admin` and
       `app_user`, then applies and verifies the schema/RLS configuration.
    5. The bootstrap task writes the generated app_user URL into app-db-url.
       Force a new deployment of the api/worker services to load it:
         aws ecs update-service --cluster <cluster> --service <service> --force-new-deployment
    6. Retrieve acceptance_fixtures_secret_arn into a protected local
       tenant_fixtures.json file. The bootstrap task rotates those synthetic
       tenant keys on every run; retrieve the latest secret after each run.
    7. Check `/readyz` on the ALB; it should return 200.
  EOT
}
