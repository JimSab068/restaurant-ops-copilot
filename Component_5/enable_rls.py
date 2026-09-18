"""
enable_rls.py — Configure PostgreSQL Row-Level Security for Restaurant Ops Copilot.

Architecture
------------
postgres
    Bootstrap/database administration only.

rls_admin
    Non-superuser administrative/configuration role.
    Owns application tables and can perform controlled schema/configuration work.
    IMPORTANT: FORCE ROW LEVEL SECURITY means this role is still subject to RLS.

app_user
    Runtime application role.
    Subject to RLS and receives CRUD privileges on application tables.

Security model
--------------
Every tenant-scoped table is protected by a tenant-isolation policy.

The policy compares:

    tenant_id = current_setting('app.current_tenant', true)

The tenants table is special:

    id = current_setting('app.current_tenant', true)

If app.current_tenant is unset or empty, no tenant rows are accessible.

This module deliberately does NOT:
    - grant BYPASSRLS
    - grant SUPERUSER
    - disable FORCE ROW LEVEL SECURITY
    - create an unrestricted administrative RLS policy

Usage
-----
Configure RLS:

    python -m Component_5.enable_rls

Verify RLS:

    python -m Component_5.enable_rls --verify

Environment
-----------
RLS_ADMIN_DATABASE_URL
    PostgreSQL connection used for RLS configuration.

    Example:
        postgresql://rls_admin:1234@localhost:5432/ops_copilot

APP_DB_ROLE
    Runtime application role.

    Default:
        app_user
"""

from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_DATABASE_URL = "postgresql://rls_admin:1234@localhost:5432/ops_copilot"

APP_ROLE = os.environ.get("APP_DB_ROLE", "app_user")

# Both roles remain subject to RLS.
#
# app_user:
#     actual application/runtime role
#
# rls_admin:
#     configuration/test-seeding role
#
# FORCE ROW LEVEL SECURITY means rls_admin cannot bypass the policies simply
# because it owns the tables.
POLICY_ROLES = (
    APP_ROLE,
    "rls_admin",
)

# Remove accidental duplicates while preserving order.
POLICY_ROLES = tuple(dict.fromkeys(POLICY_ROLES))

DATABASE_URL = os.environ.get(
    "RLS_ADMIN_DATABASE_URL",
    os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
)

TENANT_TABLE = "tenants"

TENANT_SCOPED_TABLES = (
    "suppliers",
    "ingredients",
    "supplier_skus",
    "supplier_prices",
    "menu_items",
    "recipes",
    "recipe_ingredients",
    "inventory_levels",
    "invoices",
    "invoice_lines",
    "sales_orders",
    "sales_order_lines",
    "staff_shifts",
    "order_volume",
    "waste_records",
    "decisions",
    "events",
    "tool_execution_log",
)

ALL_RLS_TABLES = (
    TENANT_TABLE,
    *TENANT_SCOPED_TABLES,
)

POLICY_NAME = "tenant_isolation"


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------

def _validate_identifier(value: str, name: str) -> str:
    """
    Validate identifiers before interpolating them into SQL.

    SQLAlchemy parameters cannot be used for identifiers such as table names
    or role names, so these values must be validated before interpolation.
    """
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")

    if not value:
        raise ValueError(f"{name} cannot be empty")

    if not value.replace("_", "").isalnum():
        raise ValueError(
            f"Invalid SQL identifier for {name!r}: {value!r}"
        )

    if not (value[0].isalpha() or value[0] == "_"):
        raise ValueError(
            f"Invalid SQL identifier for {name!r}: {value!r}"
        )

    return value


def _validated_roles() -> tuple[str, ...]:
    return tuple(
        _validate_identifier(role, "policy role")
        for role in POLICY_ROLES
    )


def _policy_roles_sql() -> str:
    """
    Produce:

        app_user, rls_admin

    after validating each identifier.
    """
    return ", ".join(_validated_roles())


# ---------------------------------------------------------------------------
# Database role validation
# ---------------------------------------------------------------------------

def _role_exists(conn: Connection, role: str) -> bool:
    result = conn.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_roles
                WHERE rolname = :role
            )
            """
        ),
        {"role": role},
    )
    return bool(result.scalar_one())


def _validate_role_security_properties(
    conn: Connection,
    role: str,
) -> None:
    """
    Ensure the role cannot bypass RLS through PostgreSQL role attributes.
    """
    result = conn.execute(
        text(
            """
            SELECT
                rolsuper,
                rolbypassrls,
                rolcanlogin
            FROM pg_roles
            WHERE rolname = :role
            """
        ),
        {"role": role},
    ).mappings().one()

    if result["rolsuper"]:
        raise RuntimeError(
            f"Refusing to configure RLS with SUPERUSER role: {role!r}"
        )

    if result["rolbypassrls"]:
        raise RuntimeError(
            f"Refusing to configure RLS with BYPASSRLS role: {role!r}"
        )

    if not result["rolcanlogin"]:
        raise RuntimeError(
            f"Role {role!r} cannot LOGIN; check your database role setup."
        )


def _validate_configuration_role(conn: Connection) -> None:
    """
    The role executing this script must itself not bypass RLS.

    This is important because the tables use FORCE ROW LEVEL SECURITY.
    """
    current_user = conn.execute(
        text("SELECT current_user")
    ).scalar_one()

    if current_user == "postgres":
        raise RuntimeError(
            "Refusing to configure RLS while connected as the PostgreSQL "
            "superuser 'postgres'. Use the non-superuser 'rls_admin' role."
        )

    _validate_role_security_properties(conn, current_user)


def _validate_roles(conn: Connection) -> None:
    for role in POLICY_ROLES:
        _validate_identifier(role, "policy role")

        if not _role_exists(conn, role):
            raise RuntimeError(
                f"Required PostgreSQL role does not exist: {role!r}"
            )

        _validate_role_security_properties(conn, role)


# ---------------------------------------------------------------------------
# Table validation
# ---------------------------------------------------------------------------

def _table_exists(conn: Connection, table_name: str) -> bool:
    result = conn.execute(
        text(
            """
            SELECT to_regclass(:table_name) IS NOT NULL
            """
        ),
        {"table_name": f"public.{table_name}"},
    )

    return bool(result.scalar_one())


def _validate_tables(conn: Connection) -> None:
    for table_name in ALL_RLS_TABLES:
        _validate_identifier(table_name, "table name")

        if not _table_exists(conn, table_name):
            raise RuntimeError(
                f"Required table does not exist: public.{table_name}"
            )


# ---------------------------------------------------------------------------
# Policy management
# ---------------------------------------------------------------------------

def _drop_existing_policy(
    conn: Connection,
    table_name: str,
) -> None:
    """
    Drop our known policy before recreating it.

    This makes the configuration idempotent and ensures that changes to the
    policy definition are applied on subsequent runs.
    """
    table_name = _validate_identifier(table_name, "table name")
    policy_name = _validate_identifier(POLICY_NAME, "policy name")

    conn.execute(
        text(
            f"""
            DROP POLICY IF EXISTS {policy_name}
            ON public.{table_name}
            """
        )
    )


def _create_tenants_policy(conn: Connection) -> None:
    """
    tenants is special because its tenant identifier column is `id`, not
    `tenant_id`.
    """
    roles_sql = _policy_roles_sql()

    conn.execute(
        text(
            f"""
            CREATE POLICY {POLICY_NAME}
            ON public.tenants
            AS PERMISSIVE
            FOR ALL
            TO {roles_sql}
            USING (
                id = current_setting(
                    'app.current_tenant',
                    true
                )
            )
            WITH CHECK (
                id = current_setting(
                    'app.current_tenant',
                    true
                )
            )
            """
        )
    )


def _create_tenant_scoped_policy(
    conn: Connection,
    table_name: str,
) -> None:
    table_name = _validate_identifier(table_name, "table name")
    roles_sql = _policy_roles_sql()

    conn.execute(
        text(
            f"""
            CREATE POLICY {POLICY_NAME}
            ON public.{table_name}
            AS PERMISSIVE
            FOR ALL
            TO {roles_sql}
            USING (
                tenant_id = current_setting(
                    'app.current_tenant',
                    true
                )
            )
            WITH CHECK (
                tenant_id = current_setting(
                    'app.current_tenant',
                    true
                )
            )
            """
        )
    )


def _enable_force_rls(

        
    conn: Connection,
    table_name: str,
) -> None:
    table_name = _validate_identifier(table_name, "table name")

    conn.execute(
        text(
            f"""
            ALTER TABLE public.{table_name}
            ENABLE ROW LEVEL SECURITY
            """
        )
    )

    conn.execute(
        text(
            f"""
            ALTER TABLE public.{table_name}
            FORCE ROW LEVEL SECURITY
            """
        )
    )


def _grant_crud(
    conn: Connection,
    table_name: str,
) -> None:
    """
    Grant application CRUD privileges.

    RLS remains the security boundary. PostgreSQL privileges decide whether
    the role may issue the operation; RLS decides which rows are visible or
    writable.
    """
    table_name = _validate_identifier(table_name, "table name")

    conn.execute(
        text(
            f"""
            GRANT SELECT, INSERT, UPDATE, DELETE
            ON TABLE public.{table_name}
            TO {_validate_identifier(APP_ROLE, "APP_ROLE")}
            """
        )
    )


def _configure_table(
    conn: Connection,
    table_name: str,
) -> None:
    _enable_force_rls(conn, table_name)
    _drop_existing_policy(conn, table_name)

    if table_name == TENANT_TABLE:
        _create_tenants_policy(conn)
    else:
        _create_tenant_scoped_policy(conn, table_name)

    _grant_crud(conn, table_name)


# ---------------------------------------------------------------------------
# Sequence privileges
# ---------------------------------------------------------------------------

def _grant_sequence_privileges(conn: Connection) -> None:
    """
    Most IDs in this project are application-generated strings, but granting
    sequence privileges for sequences owned by these tables makes this
    configuration safer if a serial/identity column is introduced later.

    No sequence privileges are granted blindly to every sequence in the DB.
    """
    conn.execute(
        text(
            f"""
            DO $$
            DECLARE
                seq_name text;
            BEGIN
                FOR seq_name IN
                    SELECT DISTINCT
                        format(
                            '%I.%I',
                            n.nspname,
                            c.relname
                        )
                    FROM pg_class c
                    JOIN pg_namespace n
                        ON n.oid = c.relnamespace
                    JOIN pg_depend d
                        ON d.objid = c.oid
                    JOIN pg_class t
                        ON t.oid = d.refobjid
                    WHERE c.relkind = 'S'
                      AND n.nspname = 'public'
                      AND t.relname = ANY (
                          ARRAY[
                              'tenants',
                              'suppliers',
                              'ingredients',
                              'supplier_skus',
                              'supplier_prices',
                              'menu_items',
                              'recipes',
                              'recipe_ingredients',
                              'inventory_levels',
                              'invoices',
                              'invoice_lines',
                              'sales_orders',
                              'sales_order_lines',
                              'staff_shifts',
                              'order_volume',
                              'waste_records',
                              'decisions',
                              'events',
                              'tool_execution_log'
                          ]
                      )
                LOOP
                    EXECUTE format(
                        'GRANT USAGE, SELECT, UPDATE ON SEQUENCE %s TO %I',
                        seq_name,
                        '{APP_ROLE}'
                    );
                END LOOP;
            END
            $$
            """
        )
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def configure_rls(engine: Engine) -> None:
    """
    Configure all RLS policies inside one transaction.

    If any table fails, PostgreSQL rolls back the entire configuration rather
    than leaving the database half-configured.
    """
    with engine.begin() as conn:
        _validate_configuration_role(conn)
        _validate_roles(conn)
        _validate_tables(conn)

        for table_name in ALL_RLS_TABLES:
            _configure_table(conn, table_name)

        _grant_sequence_privileges(conn)

    print(
        "RLS configured successfully on: "
        + ", ".join(ALL_RLS_TABLES)
    )
    print(
        f"Application role: {APP_ROLE!r}; "
        "SELECT/INSERT/UPDATE/DELETE privileges granted."
    )
    print(
        "Tenant-isolation policy applies to: "
        + ", ".join(POLICY_ROLES)
    )


# ---------------------------------------------------------------------------
# Verification helpers
# ---------------------------------------------------------------------------

def _set_tenant(
    conn: Connection,
    tenant_id: str | None,
) -> None:
    """
    Set the transaction-local tenant context.

    `true` means the setting is local to the current transaction.
    """
    value = "" if tenant_id is None else tenant_id

    conn.execute(
        text(
            """
            SELECT set_config(
                'app.current_tenant',
                :tenant_id,
                true
            )
            """
        ),
        {"tenant_id": value},
    )


def _current_tenant(
    conn: Connection,
) -> str | None:
    return conn.execute(
        text(
            """
            SELECT current_setting(
                'app.current_tenant',
                true
            )
            """
        )
    ).scalar_one_or_none()


def _verify_policy_definition(
    conn: Connection,
) -> None:
    """
    Verify every required table has RLS enabled, forced, and has our policy.
    """
    rows = conn.execute(
        text(
            """
            SELECT
                c.relname AS table_name,
                c.relrowsecurity AS rls_enabled,
                c.relforcerowsecurity AS rls_forced,
                EXISTS (
                    SELECT 1
                    FROM pg_policy p
                    WHERE p.polrelid = c.oid
                      AND p.polname = :policy_name
                ) AS has_policy
            FROM pg_class c
            JOIN pg_namespace n
                ON n.oid = c.relnamespace
            WHERE n.nspname = 'public'
              AND c.relname = ANY(:table_names)
            ORDER BY c.relname
            """
        ),
        {
            "policy_name": POLICY_NAME,
            "table_names": list(ALL_RLS_TABLES),
        },
    ).mappings().all()

    found = {row["table_name"]: row for row in rows}

    for table_name in ALL_RLS_TABLES:
        row = found.get(table_name)

        if row is None:
            raise AssertionError(
                f"RLS verification failed: table {table_name!r} missing."
            )

        if not row["rls_enabled"]:
            raise AssertionError(
                f"RLS verification failed: "
                f"{table_name!r} does not have RLS enabled."
            )

        if not row["rls_forced"]:
            raise AssertionError(
                f"RLS verification failed: "
                f"{table_name!r} does not have FORCE RLS enabled."
            )

        if not row["has_policy"]:
            raise AssertionError(
                f"RLS verification failed: "
                f"{table_name!r} has no {POLICY_NAME!r} policy."
            )


def _verify_policy_roles(
    conn: Connection,
) -> None:
    """
    Confirm our policy explicitly targets both runtime and admin roles.
    """
    rows = conn.execute(
        text(
            """
            SELECT
                tablename,
                policyname,
                roles
            FROM pg_policies
            WHERE schemaname = 'public'
              AND policyname = :policy_name
              AND tablename = ANY(:table_names)
            ORDER BY tablename
            """
        ),
        {
            "policy_name": POLICY_NAME,
            "table_names": list(ALL_RLS_TABLES),
        },
    ).mappings().all()

    expected_roles = set(POLICY_ROLES)
    found_tables = set()

    for row in rows:
        found_tables.add(row["tablename"])
        actual_roles = set(row["roles"])

        if not expected_roles.issubset(actual_roles):
            raise AssertionError(
                f"Policy role verification failed for "
                f"{row['tablename']!r}: "
                f"expected {sorted(expected_roles)}, "
                f"got {sorted(actual_roles)}"
            )

    missing_tables = set(ALL_RLS_TABLES) - found_tables
    if missing_tables:
        raise AssertionError(
            "Policy role verification failed; missing policy rows for "
            + ", ".join(sorted(missing_tables))
        )


def _verify_fail_closed(
    engine: Engine,
) -> None:
    """
    Verify that a connection with no tenant context cannot see tenant data.

    This specifically uses a fresh connection so we do not accidentally
    inherit a tenant setting from an earlier transaction.
    """
    with engine.connect() as conn:
        # Explicitly clear the setting in this connection.
        conn.execute(
            text(
                """
                SELECT set_config(
                    'app.current_tenant',
                    '',
                    false
                )
                """
            )
        )

        current = _current_tenant(conn)

        if current not in ("", None):
            raise AssertionError(
                "Expected empty/unset tenant context, "
                f"got {current!r}"
            )

        count = conn.execute(
            text(
                """
                SELECT COUNT(*)
                FROM public.tenants
                """
            )
        ).scalar_one()

        if count != 0:
            raise AssertionError(
                "FAIL-CLOSED verification failed: "
                f"tenantless connection can see {count} tenant rows."
            )


def _verify_tenant_visibility(
    engine: Engine,
    tenant_a: str,
    tenant_b: str,
) -> None:
    """
    Verify that tenant A sees only A and tenant B sees only B.

    This assumes the test database already contains the supplied tenants.
    """
    with engine.connect() as conn:
        transaction = conn.begin()

        try:
            _set_tenant(conn, tenant_a)

            visible_a = conn.execute(
                text(
                    """
                    SELECT id
                    FROM public.tenants
                    ORDER BY id
                    """
                )
            ).scalars().all()

            if visible_a != [tenant_a]:
                raise AssertionError(
                    "Tenant A isolation failed: "
                    f"expected [{tenant_a!r}], "
                    f"got {visible_a!r}"
                )

            _set_tenant(conn, tenant_b)

            visible_b = conn.execute(
                text(
                    """
                    SELECT id
                    FROM public.tenants
                    ORDER BY id
                    """
                )
            ).scalars().all()

            if visible_b != [tenant_b]:
                raise AssertionError(
                    "Tenant B isolation failed: "
                    f"expected [{tenant_b!r}], "
                    f"got {visible_b!r}"
                )

            transaction.commit()

        except Exception:
            transaction.rollback()
            raise


def verify_rls(engine: Engine) -> None:
    """
    Run structural and fail-closed RLS verification.

    Important:
    This function does not create tenants. It only verifies the installed
    configuration and fail-closed behavior.
    """
    with engine.connect() as conn:
        _validate_configuration_role(conn)
        _validate_roles(conn)
        _validate_tables(conn)
        _verify_policy_definition(conn)
        _verify_policy_roles(conn)

    _verify_fail_closed(engine)

    print("RLS policy structure verified successfully.")
    print("RLS fail-closed behavior verified successfully.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_engine() -> Engine:
    if not DATABASE_URL:
        raise RuntimeError(
            "No database URL configured. Set RLS_ADMIN_DATABASE_URL."
        )

    return create_engine(
        DATABASE_URL,
        pool_pre_ping=True,
        future=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Configure and verify PostgreSQL RLS."
    )

    parser.add_argument(
        "--verify",
        action="store_true",
        help="Verify the existing RLS configuration instead of modifying it.",
    )

    args = parser.parse_args()

    try:
        engine = _build_engine()

        if args.verify:
            verify_rls(engine)
        else:
            configure_rls(engine)

        engine.dispose()
        return 0

    except Exception as exc:
        print(
            f"RLS operation failed: {exc}",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
