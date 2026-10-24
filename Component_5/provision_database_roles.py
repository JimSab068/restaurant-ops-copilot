"""Provision the non-superuser roles required by the application bootstrap.

The bootstrap task connects with the database master account to create or
rotate `rls_admin` and `app_user`. The password for `rls_admin` comes from
RLS_ADMIN_DATABASE_URL; the generated `app_user` URL is written directly to
the configured Secrets Manager secret and is never printed.
"""

from __future__ import annotations

import os
import secrets

import boto3
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, URL, make_url
from sqlalchemy.exc import ArgumentError


RLS_ROLE = "rls_admin"
APP_ROLE = "app_user"


def _required_url(name: str) -> URL:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} must be set.")
    try:
        return make_url(value)
    except ArgumentError as exc:
        raise RuntimeError(f"{name} must be a valid PostgreSQL URL.") from exc


def _validate_target(seed_url: URL, rls_url: URL) -> None:
    if seed_url.drivername.split("+", maxsplit=1)[0] != "postgresql":
        raise RuntimeError("SEED_DATABASE_URL must use PostgreSQL.")
    if rls_url.drivername.split("+", maxsplit=1)[0] != "postgresql":
        raise RuntimeError("RLS_ADMIN_DATABASE_URL must use PostgreSQL.")
    if rls_url.username != RLS_ROLE or not rls_url.password:
        raise RuntimeError(
            f"RLS_ADMIN_DATABASE_URL must contain the {RLS_ROLE!r} username "
            "and its password."
        )
    if not seed_url.host or not seed_url.database:
        raise RuntimeError(
            "SEED_DATABASE_URL must include a database host and database name."
        )
    if (
        seed_url.host.lower() != (rls_url.host or "").lower()
        or (seed_url.port or 5432) != (rls_url.port or 5432)
        or seed_url.database != rls_url.database
    ):
        raise RuntimeError(
            "SEED_DATABASE_URL and RLS_ADMIN_DATABASE_URL must target the "
            "same database host, port, and database name."
        )


def _execute_role_statement(
    connection: Connection,
    role_name: str,
    password: str,
) -> None:
    exists = connection.execute(
        text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role_name)"),
        {"role_name": role_name},
    ).scalar_one()
    operation = "ALTER ROLE" if exists else "CREATE ROLE"
    statement = connection.execute(
        text(
            "SELECT format(:operation || ' %I WITH LOGIN PASSWORD %L NOSUPERUSER "
            "NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS', "
            ":role_name, :password)"
        ),
        {
            "operation": operation,
            "role_name": role_name,
            "password": password,
        },
    ).scalar_one()
    connection.exec_driver_sql(statement)


def _require_role_management_privilege(connection: Connection) -> None:
    can_create_roles = connection.execute(
        text(
            "SELECT rolcreaterole FROM pg_roles "
            "WHERE rolname = current_user"
        )
    ).scalar_one()
    if not can_create_roles:
        raise RuntimeError(
            "SEED_DATABASE_URL must connect as the database master or another "
            "role with PostgreSQL CREATEROLE privilege."
        )


def _existing_app_password(
    secrets_client,
    secret_arn: str,
    seed_url: URL,
) -> str | None:
    secret = secrets_client.get_secret_value(SecretId=secret_arn)
    try:
        existing_url = make_url(secret["SecretString"])
    except ArgumentError:
        return None

    if (
        existing_url.username == APP_ROLE
        and existing_url.password
        and existing_url.host
        and seed_url.host
        and existing_url.host.lower() == seed_url.host.lower()
        and (existing_url.port or 5432) == (seed_url.port or 5432)
        and existing_url.database == seed_url.database
    ):
        return existing_url.password
    return None


def provision_roles() -> None:
    seed_url = _required_url("SEED_DATABASE_URL")
    rls_url = _required_url("RLS_ADMIN_DATABASE_URL")
    secret_arn = os.environ.get("APP_DB_URL_SECRET_ARN")
    if not secret_arn:
        raise RuntimeError("APP_DB_URL_SECRET_ARN must be set for role provisioning.")
    _validate_target(seed_url, rls_url)

    region = os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-east-2"
    )
    secrets_client = boto3.client("secretsmanager", region_name=region)
    app_password = _existing_app_password(secrets_client, secret_arn, seed_url)
    if app_password is None:
        app_password = secrets.token_urlsafe(32)
    app_url = seed_url.set(username=APP_ROLE, password=app_password)

    engine = create_engine(seed_url)
    try:
        with engine.begin() as connection:
            _require_role_management_privilege(connection)
            _execute_role_statement(connection, RLS_ROLE, rls_url.password)
            _execute_role_statement(connection, APP_ROLE, app_password)

            quoted_database = (
                connection.dialect.identifier_preparer.quote_identifier(
                    seed_url.database
                )
            )
            connection.exec_driver_sql(
                f"GRANT CONNECT ON DATABASE {quoted_database} "
                f"TO {RLS_ROLE}, {APP_ROLE}"
            )
            connection.exec_driver_sql(
                f"GRANT USAGE, CREATE ON SCHEMA public TO {RLS_ROLE}"
            )
            connection.exec_driver_sql(
                f"GRANT USAGE ON SCHEMA public TO {APP_ROLE}"
            )
    finally:
        engine.dispose()

    secrets_client.put_secret_value(
        SecretId=secret_arn,
        SecretString=app_url.render_as_string(hide_password=False),
    )


def main() -> int:
    provision_roles()
    print("Database roles provisioned; app_user URL stored in Secrets Manager.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
