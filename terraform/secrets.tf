# ---------------------------------------------------------------------------
# app_db_url: the application's real runtime DATABASE_URL, using app_user
# (NOT the master/rls_admin credentials). Terraform creates the SECRET
# container here; the bootstrap task (ecs.tf) is responsible for actually
# creating the app_user Postgres role and then writing its real connection
# string into this secret's value via the AWS CLI/SDK as its last step --
# Terraform cannot know app_user's password because that's created inside
# Postgres by Component_5's bootstrap script, not by Terraform.
#
# Until the bootstrap task runs once, this secret holds a placeholder and
# the api/worker services will fail /readyz -- that's intentional: it
# should be impossible for the app services to come up pointing at
# nonexistent credentials.
# ---------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "app_db_url" {
  name = "${local.name_prefix}/app-db-url"
}

resource "aws_secretsmanager_secret_version" "app_db_url_placeholder" {
  secret_id     = aws_secretsmanager_secret.app_db_url.id
  secret_string = "REPLACE_ME_RUN_BOOTSTRAP_TASK_FIRST"

  lifecycle {
    ignore_changes = [secret_string] # bootstrap task overwrites this out-of-band
  }
}

resource "random_password" "api_key_pepper" {
  length  = 64
  special = false
}

resource "aws_secretsmanager_secret" "api_key_pepper" {
  name = "${local.name_prefix}/api-key-pepper"
}

resource "aws_secretsmanager_secret_version" "api_key_pepper" {
  secret_id     = aws_secretsmanager_secret.api_key_pepper.id
  secret_string = random_password.api_key_pepper.result
}

resource "aws_secretsmanager_secret" "acceptance_fixtures" {
  name = "${local.name_prefix}/acceptance-fixtures"
}

resource "aws_secretsmanager_secret" "release_gate_reader_url" {
  name = "${local.name_prefix}/release-gate-reader-url"
}

resource "aws_secretsmanager_secret_version" "release_gate_reader_url_placeholder" {
  secret_id     = aws_secretsmanager_secret.release_gate_reader_url.id
  secret_string = "RUN_RELEASE_GATE_READER_SETUP_TASK_FIRST"

  lifecycle {
    ignore_changes = [secret_string]
  }
}

resource "aws_secretsmanager_secret_version" "acceptance_fixtures_placeholder" {
  secret_id     = aws_secretsmanager_secret.acceptance_fixtures.id
  secret_string = "RUN_BOOTSTRAP_TASK_FIRST"

  lifecycle {
    ignore_changes = [secret_string]
  }
}
