# ---------------------------------------------------------------------------
# Phase 5 (part 2): task roles scoped to RDS/Secrets Manager/CloudWatch/
# Bedrock only -- no AWS keys in environment variables anywhere.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

# --- Execution role: pulls the image, writes logs, reads secrets into env ---
# (This is distinct from the task role below -- execution role is used by
# the ECS agent itself before your code runs; task role is what your code
# runs as.)

resource "aws_iam_role" "execution" {
  name               = "${local.name_prefix}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_managed" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "execution_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.db_master_url.arn,
      aws_secretsmanager_secret.db_master.arn,
      aws_secretsmanager_secret.app_db_url.arn,
      aws_secretsmanager_secret.api_key_pepper.arn,
      var.rls_admin_database_url_secret_arn,
      aws_secretsmanager_secret.release_gate_reader_url.arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  name   = "${local.name_prefix}-execution-secrets"
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.execution_secrets.json
}

# --- API task role ---

data "aws_iam_policy_document" "api_task" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.api.arn}:*"]
  }

  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"] # PutMetricData does not support resource-level scoping
  }

  statement {
    sid       = "Queue"
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.work_requests.arn]
  }

  statement {
    sid     = "Bedrock"
    actions = ["bedrock:InvokeModel", "bedrock:Converse", "bedrock:ConverseStream"]
    resources = [
      "arn:aws:bedrock:${var.bedrock_region}::foundation-model/*"
    ]
  }
}

resource "aws_iam_role" "api_task" {
  name               = "${local.name_prefix}-api-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "api_task" {
  name   = "${local.name_prefix}-api-task"
  role   = aws_iam_role.api_task.id
  policy = data.aws_iam_policy_document.api_task.json
}

# --- Worker task role: SQS consume + Bedrock invocation ---

data "aws_iam_policy_document" "worker_task" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.worker.arn}:*"]
  }

  statement {
    sid       = "Metrics"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
  }

  statement {
    sid       = "Queue"
    actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
    resources = [aws_sqs_queue.work_requests.arn]
  }

  statement {
    sid     = "Bedrock"
    actions = ["bedrock:InvokeModel", "bedrock:Converse", "bedrock:ConverseStream"]
    resources = [
      "arn:aws:bedrock:${var.bedrock_region}::foundation-model/*"
    ]
  }
}

resource "aws_iam_role" "worker_task" {
  name               = "${local.name_prefix}-worker-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "worker_task" {
  name   = "${local.name_prefix}-worker-task"
  role   = aws_iam_role.worker_task.id
  policy = data.aws_iam_policy_document.worker_task.json
}

# --- Bootstrap task role: the ONE place that needs master-password access,
# scoped tightly and used only for the one-shot migration/RLS task. ---

data "aws_iam_policy_document" "bootstrap_task" {
  statement {
    sid       = "Logs"
    actions   = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = ["${aws_cloudwatch_log_group.bootstrap.arn}:*"]
  }

  # Scoped to exactly one secret -- the bootstrap task can overwrite the
  # app_user connection string it just generated, and nothing else. It
  # cannot read or write db-master-password, db-master-url, or
  # api-key-pepper (compare: execution_secrets' GetSecretValue list, which
  # includes those because the task needs the MASTER url injected as
  # RLS_ADMIN_DATABASE_URL to run migrations -- that's read-only exposure
  # via the execution role's secrets injection, not this task role).
  statement {
    sid = "WriteAppDbUrlOnly"
    actions = [
      "secretsmanager:GetSecretValue",
      "secretsmanager:PutSecretValue",
      "secretsmanager:DescribeSecret",
    ]
    resources = [aws_secretsmanager_secret.app_db_url.arn]
  }

  statement {
    sid       = "PublishAcceptanceFixtures"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue"]
    resources = [aws_secretsmanager_secret.acceptance_fixtures.arn]
  }
}

resource "aws_iam_role" "bootstrap_task" {
  name               = "${local.name_prefix}-bootstrap-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "bootstrap_task" {
  name   = "${local.name_prefix}-bootstrap-task"
  role   = aws_iam_role.bootstrap_task.id
  policy = data.aws_iam_policy_document.bootstrap_task.json
}

data "aws_iam_policy_document" "release_gate_setup_task" {
  statement {
    sid       = "WriteReleaseGateReaderCredential"
    actions   = ["secretsmanager:PutSecretValue", "secretsmanager:DescribeSecret"]
    resources = [aws_secretsmanager_secret.release_gate_reader_url.arn]
  }
}

resource "aws_iam_role" "release_gate_setup_task" {
  name               = "${local.name_prefix}-release-gate-setup-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy" "release_gate_setup_task" {
  name   = "${local.name_prefix}-release-gate-setup-task"
  role   = aws_iam_role.release_gate_setup_task.id
  policy = data.aws_iam_policy_document.release_gate_setup_task.json
}
