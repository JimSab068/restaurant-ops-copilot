"""
Component_5/create_release_gate_reader.py

Creates (or rotates) the read-only, tenant-scoped Postgres role
deployed_system_acceptance.py's release_gates() needs. This is deliberately
NOT app_user (RLS-scoped, read-write) and NOT rls_admin (full DDL, used only
for migrations) -- it's a third, narrower role:

    - LOGIN, with a generated password
    - NOBYPASSRLS, with an explicit tenant policy on only the four tables
      needed by the release gates
    - SELECT only, on exactly the tables the release gates query. No
      INSERT/UPDATE/DELETE, no DDL, no access to api_credentials (it has no
      business reading API key hashes).

Idempotent: safe to run again later (e.g. after adding a table to the grant
list) without erroring if the role already exists -- it updates the
password and grants in place rather than failing on a duplicate CREATE ROLE.

Usage (local; set both URLs to the same database. The policy URL may use
RELEASE_GATE_POLICY_DATABASE_URL or the existing RLS_ADMIN_DATABASE_URL name):
    RELEASE_GATE_ADMIN_DATABASE_URL=postgresql://postgres:<password>@127.0.0.1:5433/ops_copilot
    export RLS_ADMIN_DATABASE_URL=postgresql://postgres:...@localhost:5432/ops_copilot
    python -m Component_5.create_release_gate_reader
    # prints the connection string once -- your own terminal, not a shipped
    # log, so printing here is fine; copy it into OPS_COPILOT_RELEASE_GATE_DATABASE_URL

Usage (bootstrap task on AWS -- see ecs.tf):
    RELEASE_GATE_ADMIN_DATABASE_URL, RELEASE_GATE_POLICY_DATABASE_URL, and
    RELEASE_GATE_READER_SECRET_ARN are supplied by the task environment. The
    RDS master connection creates the role; rls_admin grants SELECT and
    creates tenant policies. The URL is written to Secrets Manager, never
    printed or logged.
"""
from __future__ import annotations

import os
import secrets

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

ROLE_NAME = "release_gate_reader"

# Exactly what deployed_system_acceptance.py's release_gates() queries.
# Add a table here only when a gate actually needs to read it.
_GRANT_TABLES = (
    "work_requests",
    "decisions",
    "tool_execution_log",
    "evaluation_records",
)
_PERMISSIVE_POLICY_NAME = "release_gate_reader_tenant_allow"
_RESTRICTIVE_POLICY_NAME = "release_gate_reader_tenant_guard"


def _admin_url() -> str:
    url = os.environ.get("RELEASE_GATE_ADMIN_DATABASE_URL")
    if not url:
        raise RuntimeError(
            "RELEASE_GATE_ADMIN_DATABASE_URL must be set to an admin connection "
            "for the database that should receive release_gate_reader."
        )
    return url


def _db_name_from_url(url: str) -> str:
    # postgresql://user:pass@host:5432/dbname -> dbname
    database = make_url(url).database
    if not database:
        raise RuntimeError("Database URL must include a database name.")
    return database


def _policy_url() -> URL:
    value = os.environ.get("RELEASE_GATE_POLICY_DATABASE_URL") or os.environ.get(
        "RLS_ADMIN_DATABASE_URL"
    )
    if not value:
        raise RuntimeError("RELEASE_GATE_POLICY_DATABASE_URL must be set.")
    return make_url(value)


def _execute_role_statement(conn, password: str) -> None:
    exists = conn.execute(
        text("SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :name)"),
        {"name": ROLE_NAME},
    ).scalar_one()
    operation = "ALTER ROLE" if exists else "CREATE ROLE"
    statement = conn.execute(
        text(
            "SELECT format(:operation || ' %I WITH LOGIN PASSWORD %L "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOBYPASSRLS', "
            ":role_name, :password)"
        ),
        {
            "operation": operation,
            "role_name": ROLE_NAME,
            "password": password,
        },
    ).scalar_one()
    conn.exec_driver_sql(statement)


def _provision_reader_role(admin_url: URL, password: str) -> None:
    engine = create_engine(admin_url)
    try:
        with engine.begin() as conn:
            can_create_roles = conn.execute(
                text(
                    "SELECT rolcreaterole FROM pg_roles "
                    "WHERE rolname = current_user"
                )
            ).scalar_one()
            if not can_create_roles:
                raise RuntimeError(
                    "RELEASE_GATE_ADMIN_DATABASE_URL must connect as the "
                    "RDS master user or another role with CREATEROLE."
                )

            _execute_role_statement(conn, password)
            role = conn.execute(
                text(
                    "SELECT rolsuper, rolbypassrls FROM pg_roles "
                    "WHERE rolname = :name"
                ),
                {"name": ROLE_NAME},
            ).one()
            if role.rolsuper or role.rolbypassrls:
                raise RuntimeError(
                    "release_gate_reader must be non-superuser and "
                    "NOBYPASSRLS."
                )

            quoted_db_name = conn.dialect.identifier_preparer.quote_identifier(
                admin_url.database
            )
            conn.execute(
                text(f"GRANT CONNECT ON DATABASE {quoted_db_name} TO {ROLE_NAME}")
            )
    finally:
        engine.dispose()


def _configure_reader_policies(policy_url: URL) -> None:
    engine = create_engine(policy_url)
    try:
        with engine.begin() as conn:
            current_user = conn.execute(text("SELECT current_user")).scalar_one()
            if current_user != "rls_admin":
                raise RuntimeError(
                    "RELEASE_GATE_POLICY_DATABASE_URL must connect as "
                    "rls_admin, the owner of the tenant-scoped tables."
                )
            conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {ROLE_NAME}"))

            for table in _GRANT_TABLES:
                rls_state = conn.execute(
                    text(
                        """
                        SELECT relation.relrowsecurity, relation.relforcerowsecurity
                        FROM pg_class AS relation
                        JOIN pg_namespace AS ns
                          ON ns.oid = relation.relnamespace
                        WHERE ns.nspname = 'public'
                          AND relation.relname = :table
                        """
                    ),
                    {"table": table},
                ).one_or_none()
                if rls_state is None or not all(rls_state):
                    raise RuntimeError(
                        f"public.{table} must have ENABLE and FORCE ROW LEVEL SECURITY."
                    )

                conn.execute(
                    text(
                        f"REVOKE ALL PRIVILEGES ON TABLE public.{table} "
                        f"FROM {ROLE_NAME}"
                    )
                )
                conn.execute(
                    text(
                        f"GRANT SELECT ON TABLE public.{table} TO {ROLE_NAME}"
                    )
                )
                for policy_name in (
                    _PERMISSIVE_POLICY_NAME,
                    _RESTRICTIVE_POLICY_NAME,
                ):
                    conn.execute(
                        text(
                            f"DROP POLICY IF EXISTS {policy_name} "
                            f"ON public.{table}"
                        )
                    )
                for policy_name, policy_type in (
                    (_PERMISSIVE_POLICY_NAME, "PERMISSIVE"),
                    (_RESTRICTIVE_POLICY_NAME, "RESTRICTIVE"),
                ):
                    conn.execute(
                        text(
                            f"""
                            CREATE POLICY {policy_name}
                            ON public.{table}
                            AS {policy_type}
                            FOR SELECT
                            TO {ROLE_NAME}
                            USING (
                                tenant_id = current_setting(
                                    'app.current_tenant',
                                    true
                                )
                            )
                            """
                        )
                    )
    finally:
        engine.dispose()


def create_or_rotate() -> str:
    """
    Create the role if absent, or rotate its password if present. Returns
    the full connection string -- the ONLY place this value exists as a
    Python string; callers must either print it (local/manual use) or hand
    it straight to boto3's put_secret_value (AWS path) without assigning it
    to a second variable that might end up in a traceback or a wider log.
    """
    admin_url = _admin_url()
    parsed_url = make_url(admin_url)
    db_name = _db_name_from_url(admin_url)
    policy_url = _policy_url()
    if (
        not parsed_url.host
        or not policy_url.host
        or parsed_url.host.lower() != policy_url.host.lower()
        or (parsed_url.port or 5432) != (policy_url.port or 5432)
        or parsed_url.database != policy_url.database
        or parsed_url.drivername.split("+", maxsplit=1)[0] != "postgresql"
        or policy_url.drivername.split("+", maxsplit=1)[0] != "postgresql"
    ):
        raise RuntimeError(
            "RELEASE_GATE_ADMIN_DATABASE_URL and "
            "RELEASE_GATE_POLICY_DATABASE_URL must target the same "
            "PostgreSQL host, port, and database."
        )
    password = secrets.token_urlsafe(32)

    _provision_reader_role(parsed_url, password)
    _configure_reader_policies(policy_url)

    reader_url = parsed_url.set(
        database=db_name,
        username=ROLE_NAME,
        password=password,
    )
    return reader_url.render_as_string(hide_password=False)


def main() -> int:
    connection_string = create_or_rotate()

    secret_arn = os.environ.get("RELEASE_GATE_READER_SECRET_ARN")
    if secret_arn:
        import boto3
        boto3.client("secretsmanager").put_secret_value(
            SecretId=secret_arn, SecretString=connection_string
        )
        # Deliberately no f-string with the connection string anywhere below
        # this line -- confirm by secret ARN only, never by value.
        print(f"release_gate_reader credential written to {secret_arn}")
        return 0

    print("\nOPS_COPILOT_RELEASE_GATE_DATABASE_URL=" + connection_string)
    print("\n(Printed once, your terminal only -- not logged anywhere. Copy it into your .env now.)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())