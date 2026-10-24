from __future__ import annotations

import pytest
from sqlalchemy.engine import make_url

from Component_5.provision_database_roles import (
    _execute_role_statement,
    _existing_app_password,
    _require_role_management_privilege,
    _validate_target,
)


class FakeResult:
    def __init__(self, value):
        self.value = value

    def scalar_one(self):
        return self.value


class FakeConnection:
    def __init__(self, role_exists: bool):
        self.role_exists = role_exists
        self.calls = []
        self.statements = []

    def execute(self, statement, parameters):
        self.calls.append((statement, parameters))
        if len(self.calls) == 1:
            return FakeResult(self.role_exists)
        operation = parameters["operation"]
        role = parameters["role_name"]
        return FakeResult(f"{operation} {role} WITH LOGIN")

    def exec_driver_sql(self, statement):
        self.statements.append(statement)


def test_validate_target_accepts_same_postgres_target():
    _validate_target(
        make_url("postgresql://postgres:secret@db.example:5432/ops_copilot"),
        make_url("postgresql://rls_admin:secret@db.example/ops_copilot"),
    )


def test_validate_target_rejects_database_mismatch():
    with pytest.raises(RuntimeError, match="same database host"):
        _validate_target(
            make_url("postgresql://postgres:secret@db.example/ops_copilot"),
            make_url("postgresql://rls_admin:secret@db.example/other_db"),
        )


@pytest.mark.parametrize("role_exists, expected_operation", [
    (False, "CREATE ROLE"),
    (True, "ALTER ROLE"),
])
def test_role_statement_uses_expected_create_or_alter(role_exists, expected_operation):
    connection = FakeConnection(role_exists)

    _execute_role_statement(connection, "app_user", "p'assword")

    assert connection.calls[0][1] == {"role_name": "app_user"}
    assert connection.calls[1][1] == {
        "operation": expected_operation,
        "role_name": "app_user",
        "password": "p'assword",
    }
    assert connection.statements == [f"{expected_operation} app_user WITH LOGIN"]


def test_role_provisioning_requires_role_management_privilege():
    class FakePermissionConnection:
        def execute(self, statement):
            return FakeResult(False)

    with pytest.raises(RuntimeError, match="CREATEROLE"):
        _require_role_management_privilege(FakePermissionConnection())


def test_existing_app_password_is_reused_for_same_target():
    class FakeSecretsClient:
        def get_secret_value(self, *, SecretId):
            assert SecretId == "arn:secret"
            return {
                "SecretString": (
                    "postgresql://app_user:stored-password@db.example:5432/"
                    "ops_copilot"
                )
            }

    password = _existing_app_password(
        FakeSecretsClient(),
        "arn:secret",
        make_url("postgresql://postgres:secret@db.example/ops_copilot"),
    )

    assert password == "stored-password"
