# ---------------------------------------------------------------------------
# Phase 5 (part 3): ECS cluster, API service (>=2 tasks behind the ALB),
# worker service (2 -> 20 autoscaled on queue backlog), and the one-shot
# bootstrap task from Phase 3 (migrations + enable_rls.py --verify).
# ---------------------------------------------------------------------------

resource "aws_ecs_cluster" "this" {
  name = "${local.name_prefix}-cluster"

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "api" {
  name              = "/ecs/${local.name_prefix}/api"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "worker" {
  name              = "/ecs/${local.name_prefix}/worker"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "bootstrap" {
  name              = "/ecs/${local.name_prefix}/bootstrap"
  retention_in_days = 30
}

resource "aws_cloudwatch_log_group" "release_gates" {
  name              = "/ecs/${local.name_prefix}/release-gates"
  retention_in_days = 30
}

# Common environment every task needs. SYNTHETIC_EXECUTION_ONLY is a
# plain (non-secret) env var -- deliberately visible in the task
# definition so it's obvious in the console whether a given deploy has
# the kill-switch on.
locals {
  common_environment = [
    { name = "AWS_REGION", value = var.aws_region },
    { name = "SQS_QUEUE_URL", value = aws_sqs_queue.work_requests.url },
    { name = "SYNTHETIC_EXECUTION_ONLY", value = tostring(var.synthetic_execution_only) },
    { name = "APP_ENV", value = var.environment },
    { name = "COMPONENT3_BEDROCK_REGION", value = var.bedrock_region },
  ]

  common_secrets = [
    { name = "DATABASE_URL", valueFrom = aws_secretsmanager_secret.app_db_url.arn },
    { name = "API_KEY_PEPPER", valueFrom = aws_secretsmanager_secret.api_key_pepper.arn },
  ]
}

# --- API service ---

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api_task.arn

  container_definitions = jsonencode([{
    name         = "api"
    image        = var.api_image
    essential    = true
    portMappings = [{ containerPort = 8000, protocol = "tcp" }]
    command      = ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
    environment  = local.common_environment
    secrets      = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.api.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "api"
      }
    }
  }])
}

resource "aws_ecs_service" "api" {
  name            = "${local.name_prefix}-api"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.api.arn
  desired_count   = var.api_desired_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [for s in aws_subnet.private : s.id]
    security_groups  = [aws_security_group.ecs_api.id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  depends_on = [aws_lb_listener.api_https, aws_lb_listener.api_http_direct]
}

# --- Worker service ---

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker_task.arn

  container_definitions = jsonencode([{
    name        = "worker"
    image       = var.api_image
    essential   = true
    command     = ["python", "-m", "worker.main"]
    environment = local.common_environment
    secrets     = local.common_secrets
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.worker.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "worker"
      }
    }
  }])
}

resource "aws_ecs_service" "worker" {
  name            = "${local.name_prefix}-worker"
  cluster         = aws_ecs_cluster.this.id
  task_definition = aws_ecs_task_definition.worker.arn
  desired_count   = var.worker_min_count
  launch_type     = "FARGATE"

  network_configuration {
    subnets          = [for s in aws_subnet.private : s.id]
    security_groups  = [aws_security_group.ecs_worker.id]
    assign_public_ip = false
  }
}

resource "aws_appautoscaling_target" "worker" {
  service_namespace  = "ecs"
  resource_id        = "service/${aws_ecs_cluster.this.name}/${aws_ecs_service.worker.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  min_capacity       = var.worker_min_count
  max_capacity       = var.worker_max_count
}

# Scale on queue backlog (messages-per-task), not CPU -- a worker can be
# CPU-idle while still behind on a growing backlog during an LLM-latency
# spike, and backlog is the signal that actually matters here.
resource "aws_appautoscaling_policy" "worker_backlog" {
  name               = "${local.name_prefix}-worker-backlog-scaling"
  service_namespace  = aws_appautoscaling_target.worker.service_namespace
  resource_id        = aws_appautoscaling_target.worker.resource_id
  scalable_dimension = aws_appautoscaling_target.worker.scalable_dimension
  policy_type        = "TargetTrackingScaling"

  target_tracking_scaling_policy_configuration {
    target_value = 10 # target ~10 visible messages per worker task
    customized_metric_specification {
      metrics {
        label       = "Backlog per task"
        id          = "backlogPerTask"
        expression  = "visible / tasks"
        return_data = true
      }
      metrics {
        label = "Visible messages"
        id    = "visible"
        metric_stat {
          metric {
            namespace   = "AWS/SQS"
            metric_name = "ApproximateNumberOfMessagesVisible"
            dimensions {
              name  = "QueueName"
              value = aws_sqs_queue.work_requests.name
            }
          }
          stat = "Average"
        }
        return_data = false
      }
      metrics {
        label = "Running tasks"
        id    = "tasks"
        metric_stat {
          metric {
            namespace   = "ECS/ContainerInsights"
            metric_name = "RunningTaskCount"
            dimensions {
              name  = "ClusterName"
              value = aws_ecs_cluster.this.name
            }
            dimensions {
              name  = "ServiceName"
              value = aws_ecs_service.worker.name
            }
          }
          stat = "Average"
        }
        return_data = false
      }
    }
    scale_in_cooldown  = 120
    scale_out_cooldown = 30 # scale out fast, scale in slowly
  }
}

# --- Bootstrap task: run ONCE per environment, manually, after `apply` and
# before the api/worker services are expected to pass /readyz. Not part of
# a service -- invoke with:
#   aws ecs run-task --cluster <cluster> --task-definition <this family> \
#     --launch-type FARGATE --network-configuration '...'
# then update aws_secretsmanager_secret.app_db_url with the real app_user
# connection string the bootstrap script prints, before the api/worker
# services are allowed to receive traffic. ---

resource "aws_ecs_task_definition" "bootstrap" {
  family                   = "${local.name_prefix}-bootstrap"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.bootstrap_task.arn

  container_definitions = jsonencode([{
    name      = "bootstrap"
    image     = var.api_image
    essential = true
    # The same one-shot script used locally: schema, synthetic tenant seed,
    # then RLS configuration and verification.
    command = ["./bootstrap_database.sh"]
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "APP_DB_URL_SECRET_ARN", value = aws_secretsmanager_secret.app_db_url.arn },
      { name = "ACCEPTANCE_FIXTURES_SECRET_ARN", value = aws_secretsmanager_secret.acceptance_fixtures.arn },
    ]
    secrets = [
      { name = "SEED_DATABASE_URL", valueFrom = aws_secretsmanager_secret.db_master_url.arn },
      { name = "RLS_ADMIN_DATABASE_URL", valueFrom = var.rls_admin_database_url_secret_arn },
      { name = "API_KEY_PEPPER", valueFrom = aws_secretsmanager_secret.api_key_pepper.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.bootstrap.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "bootstrap"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "release_gate_setup" {
  family                   = "${local.name_prefix}-release-gate-setup"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.release_gate_setup_task.arn

  container_definitions = jsonencode([{
    name      = "release-gate-setup"
    image     = var.api_image
    essential = true
    command   = ["python", "-m", "Component_5.create_release_gate_reader"]
    environment = [
      { name = "AWS_REGION", value = var.aws_region },
      { name = "RELEASE_GATE_READER_SECRET_ARN", value = aws_secretsmanager_secret.release_gate_reader_url.arn },
    ]
    secrets = [
      { name = "RELEASE_GATE_ADMIN_DATABASE_URL", valueFrom = aws_secretsmanager_secret.db_master_url.arn },
      { name = "RELEASE_GATE_POLICY_DATABASE_URL", valueFrom = var.rls_admin_database_url_secret_arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.release_gates.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "setup"
      }
    }
  }])
}

resource "aws_ecs_task_definition" "sql_release_gates" {
  family                   = "${local.name_prefix}-sql-release-gates"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = "256"
  memory                   = "512"
  execution_role_arn       = aws_iam_role.execution.arn

  container_definitions = jsonencode([{
    name      = "sql-release-gates"
    image     = var.api_image
    essential = true
    command   = ["python", "-m", "ops.sql_release_gates"]
    secrets = [
      { name = "OPS_COPILOT_RELEASE_GATE_DATABASE_URL", valueFrom = aws_secretsmanager_secret.release_gate_reader_url.arn },
    ]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = aws_cloudwatch_log_group.release_gates.name
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "sql"
      }
    }
  }])
}
