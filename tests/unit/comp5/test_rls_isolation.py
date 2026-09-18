"""
Component 5 — PostgreSQL RLS adversarial isolation tests.

Security model
--------------
These tests verify that tenant-scoped application data cannot cross tenant
boundaries when accessed through the application database role.

Two database connections are used:

    RLS_ADMIN_DATABASE_URL
        Administrative connection used ONLY to seed/clean test data.

    RLS_TEST_DATABASE_URL
        Application/test connection used for the actual RLS security
        assertions.

IMPORTANT:
    RLS_TEST_DATABASE_URL must NOT point to postgres.

Recommended configuration:

    RLS_ADMIN_DATABASE_URL=postgresql://rls_admin:<password>@localhost:5432/ops_copilot
    RLS_TEST_DATABASE_URL=postgresql://app_user:<password>@localhost:5432/ops_copilot

The application role must not have SUPERUSER or BYPASSRLS privileges.

These tests intentionally verify fail-closed behavior:
    - no tenant context => no tenant-scoped rows
    - tenant A context => only tenant A rows
    - tenant B context => only tenant B rows
    - explicit cross-tenant predicates cannot bypass RLS
    - INSERT/UPDATE/DELETE cannot cross tenant boundaries
    - tenant context cannot be changed to access another tenant's data
"""

from __future__ import annotations

import os
from collections.abc import Generator
import hashlib

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from Component_5.enable_rls import ALL_RLS_TABLES, TENANT_SCOPED_TABLES, _set_tenant


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

ADMIN_DATABASE_URL = os.environ.get("RLS_ADMIN_DATABASE_URL")
TEST_DATABASE_URL = os.environ.get("RLS_TEST_DATABASE_URL")

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TENANT_A = "rls-tenant-a"
TENANT_B = "rls-tenant-b"

SUPPLIER_A = "rls-supplier-a"
SUPPLIER_B = "rls-supplier-b"

INGREDIENT_A = "rls-ingredient-a"
INGREDIENT_B = "rls-ingredient-b"

MENU_ITEM_A = "rls-menu-a"
MENU_ITEM_B = "rls-menu-b"

SHIFT_A = "rls-shift-a"
SHIFT_B = "rls-shift-b"

ORDER_A = "rls-order-a"
ORDER_B = "rls-order-b"

DECISION_A = "rls-decision-a"
DECISION_B = "rls-decision-b"

EVENT_A = "rls-event-a"
EVENT_B = "rls-event-b"

TOOL_LOG_A = "rls-tool-log-a"
TOOL_LOG_B = "rls-tool-log-b"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_database_urls() -> None:
    """
    Fail early with a useful message if the RLS test databases are not
    configured.
    """
    missing: list[str] = []

    if not ADMIN_DATABASE_URL:
        missing.append("RLS_ADMIN_DATABASE_URL")

    if not TEST_DATABASE_URL:
        missing.append("RLS_TEST_DATABASE_URL")

    if missing:
        pytest.skip(
            "RLS integration tests require: "
            + ", ".join(missing)
        )


# def _set_tenant(conn: Connection, tenant_id: str | None) -> None:
#     """
#     Set the current tenant in the PostgreSQL session.

#     The application RLS policies read:

#         current_setting('app.current_tenant', true)

#     An empty string is deliberately used when no tenant is supplied. This
#     allows the RLS predicate to fail closed without requiring the setting to
#     exist beforehand.
#     """
#     value = tenant_id if tenant_id is not None else ""

#     conn.execute(
#         text(
#             """
#             SELECT set_config(
#                 'app.current_tenant',
#                 :tenant_id,
#                 false
#             )
#             """
#         ),
#         {"tenant_id": value},
#     )


def _assert_only_ids(
    conn: Connection,
    table_name: str,
    expected_ids: set[str],
) -> None:
    """
    Assert that all visible rows in a tenant-scoped table are exactly the
    expected IDs.
    """
    rows = conn.execute(
        text(f"SELECT id FROM {table_name} ORDER BY id")
    ).scalars().all()

    assert set(rows) == expected_ids


def _count_rows(conn: Connection, table_name: str) -> int:
    """Return the number of rows visible to the current database role."""
    return int(
        conn.execute(
            text(f"SELECT COUNT(*) FROM {table_name}")
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def admin_engine() -> Generator[Engine, None, None]:
    """
    Administrative engine used for deterministic test setup and teardown.

    This must be rls_admin, not postgres, so the test suite does not depend
    on unrestricted superuser access.
    """
    _require_database_urls()

    assert ADMIN_DATABASE_URL is not None

    engine = create_engine(
        ADMIN_DATABASE_URL,
        future=True,
        pool_pre_ping=True,
    )

    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def app_engine() -> Generator[Engine, None, None]:
    """
    Application-role engine used by all security assertions.
    """
    _require_database_urls()

    assert TEST_DATABASE_URL is not None

    engine = create_engine(
        TEST_DATABASE_URL,
        future=True,
        pool_pre_ping=True,
    )

    try:
        yield engine
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# Database setup / teardown
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def seeded_tenants(admin_engine: Engine) -> dict[str, str]:
    """
    Seed two completely separate tenants and representative rows for every
    tenant-scoped table.

    rls_admin is subject to the same tenant_isolation RLS policy as
    app_user (see Component_5/enable_rls.py) — it does NOT have BYPASSRLS.
    So every insert/delete below must run under an explicit tenant context
    via _set_tenant(), one tenant at a time, within the same transaction.
    """

    ids_a = {
        "tenant": TENANT_A,
        "supplier": SUPPLIER_A,
        "ingredient": INGREDIENT_A,
        "menu": MENU_ITEM_A,
        "shift": SHIFT_A,
        "order": ORDER_A,
        "decision": DECISION_A,
        "event": EVENT_A,
        "tool": TOOL_LOG_A,
    }
    ids_b = {
        "tenant": TENANT_B,
        "supplier": SUPPLIER_B,
        "ingredient": INGREDIENT_B,
        "menu": MENU_ITEM_B,
        "shift": SHIFT_B,
        "order": ORDER_B,
        "decision": DECISION_B,
        "event": EVENT_B,
        "tool": TOOL_LOG_B,
    }

    def _delete_tenant_fixture_data(conn: Connection, tenant_id: str, ids: dict[str, str]) -> None:
        """Delete this tenant's fixture rows, child tables first, under its own RLS context."""
        _set_tenant(conn, tenant_id)

        conn.execute(
            text("DELETE FROM tool_execution_log WHERE id = :id"),
            {"id": ids["tool"]},
        )
        conn.execute(
            text("DELETE FROM events WHERE id = :id"),
            {"id": ids["event"]},
        )
        conn.execute(
            text("DELETE FROM order_volume WHERE id = :id"),
            {"id": ids["order"]},
        )
        conn.execute(
            text("DELETE FROM staff_shifts WHERE id = :id"),
            {"id": ids["shift"]},
        )
        conn.execute(
            text("DELETE FROM decisions WHERE id = :id"),
            {"id": ids["decision"]},
        )
        conn.execute(
            text("DELETE FROM ingredients WHERE id = :id"),
            {"id": ids["ingredient"]},
        )
        conn.execute(
            text("DELETE FROM menu_items WHERE id = :id"),
            {"id": ids["menu"]},
        )
        conn.execute(
            text("DELETE FROM suppliers WHERE id = :id"),
            {"id": ids["supplier"]},
        )
        conn.execute(
            text("DELETE FROM tenants WHERE id = :id"),
            {"id": ids["tenant"]},
        )

    def _insert_tenant_fixture_data(
        conn: Connection,
        ids: dict[str, str],
        *,
        tenant_name: str,
        supplier_name: str,
        ingredient_name: str,
        ingredient_price: float,
        ingredient_stock: float,
        menu_name: str,
        menu_price: float,
        shift_day: int,
        shift_headcount: int,
        order_quantity: int,
    ) -> None:
        """Insert one tenant's full fixture row set under its own RLS context."""
        _set_tenant(conn, ids["tenant"])

        conn.execute(
            text(
                """
                INSERT INTO tenants (
                    id,
                    name,
                    created_at
                )
                VALUES (
                    :id,
                    :name,
                    CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "id": ids["tenant"],
                "name": tenant_name,
            },
        )

        conn.execute(
            text(
                """
                INSERT INTO suppliers (
                    id,
                    tenant_id,
                    name
                )
                VALUES (
                    :id,
                    :tenant_id,
                    :name
                )
                """
            ),
            {
                "id": ids["supplier"],
                "tenant_id": ids["tenant"],
                "name": supplier_name,
            },
        )

        conn.execute(
            text(
                """
                INSERT INTO ingredients (
                    id,
                    tenant_id,
                    name,
                    unit,
                    current_price,
                    current_stock_level
                )
                VALUES (
                    :id,
                    :tenant_id,
                    :name,
                    :unit,
                    :current_price,
                    :current_stock_level
                )
                """
            ),
            {
                "id": ids["ingredient"],
                "tenant_id": ids["tenant"],
                "name": ingredient_name,
                "unit": "kg",
                "current_price": ingredient_price,
                "current_stock_level": ingredient_stock,
            },
        )

        conn.execute(
            text(
                """
                INSERT INTO menu_items (
                    id,
                    tenant_id,
                    name,
                    current_price,
                    active
                )
                VALUES (
                    :id,
                    :tenant_id,
                    :name,
                    :current_price,
                    :active
                )
                """
            ),
            {
                "id": ids["menu"],
                "tenant_id": ids["tenant"],
                "name": menu_name,
                "current_price": menu_price,
                "active": True,
            },
        )

        conn.execute(
            text(
                """
                INSERT INTO staff_shifts (
                    id,
                    tenant_id,
                    role,
                    day_of_week,
                    headcount
                )
                VALUES (
                    :id,
                    :tenant_id,
                    :role,
                    :day_of_week,
                    :headcount
                )
                """
            ),
            {
                "id": ids["shift"],
                "tenant_id": ids["tenant"],
                "role": "server",
                "day_of_week": shift_day,
                "headcount": shift_headcount,
            },
        )

        conn.execute(
            text(
                """
                INSERT INTO order_volume (
                    id,
                    tenant_id,
                    menu_item_id,
                    timestamp,
                    quantity
                )
                VALUES (
                    :id,
                    :tenant_id,
                    :menu_item_id,
                    CURRENT_TIMESTAMP,
                    :quantity
                )
                """
            ),
            {
                "id": ids["order"],
                "tenant_id": ids["tenant"],
                "menu_item_id": ids["menu"],
                "quantity": order_quantity,
            },
        )

        conn.execute(
            text(
                """
                INSERT INTO decisions (
                    id,
                    tenant_id,
                    action_type,
                    action_payload,
                    status,
                    created_at
                )
                VALUES (
                    :id,
                    :tenant_id,
                    :action_type,
                    :action_payload,
                    :status,
                    CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "id": ids["decision"],
                "tenant_id": ids["tenant"],
                "action_type": "PRICE_CHANGE",
                "action_payload": "{}",
                "status": "PROPOSED",
            },
        )

        conn.execute(
            text(
                """
                INSERT INTO events (
                    id,
                    tenant_id,
                    event_type,
                    timestamp,
                    payload,
                    source,
                    hash,
                    previous_hash
                )
                VALUES (
                    :id,
                    :tenant_id,
                    :event_type,
                    CURRENT_TIMESTAMP,
                    :payload,
                    :source,
                    :hash,
                    :previous_hash
                )
                """
            ),
            {
                "id": ids["event"],
                "tenant_id": ids["tenant"],
                "event_type": "MENU_PRICE_CHANGE",
                "payload": "{}",
                "source": "rls_test",
                "hash": hashlib.sha256(
                    f"rls-test-event-{ids['tenant']}".encode()
                ).hexdigest(),
                "previous_hash": hashlib.sha256(
                    b"GENESIS"
                ).hexdigest(),            
                },
        )

        conn.execute(
            text(
                """
                INSERT INTO tool_execution_log (
                    id,
                    tenant_id,
                    decision_id,
                    tool_category,
                    action_type,
                    confidence_gate_passed,
                    critic_approved,
                    critic_reason,
                    executed,
                    payload,
                    created_at
                )
                VALUES (
                    :id,
                    :tenant_id,
                    :decision_id,
                    :tool_category,
                    :action_type,
                    :confidence_gate_passed,
                    :critic_approved,
                    :critic_reason,
                    :executed,
                    :payload,
                    CURRENT_TIMESTAMP
                )
                """
            ),
            {
                "id": ids["tool"],
                "tenant_id": ids["tenant"],
                "decision_id": ids["decision"],
                "tool_category": "inventory",
                "action_type": "CHECK_STOCK",
                "confidence_gate_passed": True,
                "critic_approved": True,
                "critic_reason": "RLS isolation test",
                "executed": True,
                "payload": "{}",
            },
        )

    with admin_engine.begin() as conn:
        # -----------------------------------------------------------------
        # Cleanup any leftover fixture data from a previous run, per tenant.
        # -----------------------------------------------------------------
        _delete_tenant_fixture_data(conn, TENANT_A, ids_a)
        _delete_tenant_fixture_data(conn, TENANT_B, ids_b)

        # -----------------------------------------------------------------
        # Tenant A
        # -----------------------------------------------------------------
        _insert_tenant_fixture_data(
            conn,
            ids_a,
            tenant_name="RLS Tenant A",
            supplier_name="Supplier A",
            ingredient_name="Ingredient A",
            ingredient_price=10.00,
            ingredient_stock=100.00,
            menu_name="Menu Item A",
            menu_price=20.00,
            shift_day=1,
            shift_headcount=5,
            order_quantity=10,
        )

        # -----------------------------------------------------------------
        # Tenant B
        # -----------------------------------------------------------------
        _insert_tenant_fixture_data(
            conn,
            ids_b,
            tenant_name="RLS Tenant B",
            supplier_name="Supplier B",
            ingredient_name="Ingredient B",
            ingredient_price=30.00,
            ingredient_stock=200.00,
            menu_name="Menu Item B",
            menu_price=40.00,
            shift_day=2,
            shift_headcount=8,
            order_quantity=20,
        )

    yield {
        "tenant_a": TENANT_A,
        "tenant_b": TENANT_B,
    }

    # -----------------------------------------------------------------------
    # Cleanup
    # -----------------------------------------------------------------------
    with admin_engine.begin() as conn:
        _delete_tenant_fixture_data(conn, TENANT_A, ids_a)
        _delete_tenant_fixture_data(conn, TENANT_B, ids_b)
# ---------------------------------------------------------------------------
# Fail-closed tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", TENANT_SCOPED_TABLES)
def test_no_tenant_context_returns_no_rows(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
    table_name: str,
) -> None:
    """
    Without tenant context, tenant-scoped tables must return zero rows.
    """
    del seeded_tenants

    with app_engine.connect() as conn:
        _set_tenant(conn, None)

        assert _count_rows(conn, table_name) == 0


def test_tenant_table_fails_closed_without_context(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    """
    The tenants table must also fail closed when no tenant context exists.
    """
    del seeded_tenants

    with app_engine.connect() as conn:
        _set_tenant(conn, None)

        assert _count_rows(conn, "tenants") == 0


# ---------------------------------------------------------------------------
# Positive isolation tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("table_name", "tenant_a_id", "tenant_b_id"),
    [
        ("suppliers", SUPPLIER_A, SUPPLIER_B),
        ("ingredients", INGREDIENT_A, INGREDIENT_B),
        ("menu_items", MENU_ITEM_A, MENU_ITEM_B),
        ("staff_shifts", SHIFT_A, SHIFT_B),
        ("order_volume", ORDER_A, ORDER_B),
        ("decisions", DECISION_A, DECISION_B),
        ("events", EVENT_A, EVENT_B),
        ("tool_execution_log", TOOL_LOG_A, TOOL_LOG_B),
    ],
)
def test_tenant_a_sees_only_tenant_a_rows(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
    table_name: str,
    tenant_a_id: str,
    tenant_b_id: str,
) -> None:
    """
    Tenant A must see its own row and never Tenant B's row.
    """
    assert seeded_tenants["tenant_a"] == TENANT_A
    assert seeded_tenants["tenant_b"] == TENANT_B

    with app_engine.connect() as conn:
        _set_tenant(conn, TENANT_A)

        _assert_only_ids(
            conn,
            table_name,
            {tenant_a_id},
        )

        leaked = conn.execute(
            text(
                f"""
                SELECT id
                FROM {table_name}
                WHERE id = :tenant_b_id
                """
            ),
            {"tenant_b_id": tenant_b_id},
        ).scalar_one_or_none()

        assert leaked is None


@pytest.mark.parametrize(
    ("table_name", "tenant_a_id", "tenant_b_id"),
    [
        ("suppliers", SUPPLIER_A, SUPPLIER_B),
        ("ingredients", INGREDIENT_A, INGREDIENT_B),
        ("menu_items", MENU_ITEM_A, MENU_ITEM_B),
        ("staff_shifts", SHIFT_A, SHIFT_B),
        ("order_volume", ORDER_A, ORDER_B),
        ("decisions", DECISION_A, DECISION_B),
        ("events", EVENT_A, EVENT_B),
        ("tool_execution_log", TOOL_LOG_A, TOOL_LOG_B),
    ],
)
def test_tenant_b_sees_only_tenant_b_rows(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
    table_name: str,
    tenant_a_id: str,
    tenant_b_id: str,
) -> None:
    """
    Tenant B must see its own row and never Tenant A's row.
    """
    assert seeded_tenants["tenant_a"] == TENANT_A
    assert seeded_tenants["tenant_b"] == TENANT_B

    with app_engine.connect() as conn:
        _set_tenant(conn, TENANT_B)

        _assert_only_ids(
            conn,
            table_name,
            {tenant_b_id},
        )

        leaked = conn.execute(
            text(
                f"""
                SELECT id
                FROM {table_name}
                WHERE id = :tenant_a_id
                """
            ),
            {"tenant_a_id": tenant_a_id},
        ).scalar_one_or_none()

        assert leaked is None


# ---------------------------------------------------------------------------
# Cross-tenant predicate attacks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table_name", TENANT_SCOPED_TABLES)
def test_explicit_cross_tenant_predicate_cannot_bypass_rls(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
    table_name: str,
) -> None:
    """
    Adding an explicit WHERE tenant_id = <other tenant> predicate must not
    bypass the RLS policy.
    """
    del seeded_tenants

    with app_engine.connect() as conn:
        _set_tenant(conn, TENANT_A)

        rows = conn.execute(
            text(
                f"""
                SELECT id, tenant_id
                FROM {table_name}
                WHERE tenant_id = :other_tenant
                """
            ),
            {"other_tenant": TENANT_B},
        ).all()

        assert rows == []


@pytest.mark.parametrize("table_name", TENANT_SCOPED_TABLES)
def test_or_predicate_cannot_bypass_rls(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
    table_name: str,
) -> None:
    """
    An attacker must not be able to use OR conditions to broaden the result
    beyond the current tenant.
    """
    del seeded_tenants

    with app_engine.connect() as conn:
        _set_tenant(conn, TENANT_A)

        rows = conn.execute(
            text(
                f"""
                SELECT id, tenant_id
                FROM {table_name}
                WHERE tenant_id = :tenant_a
                   OR tenant_id = :tenant_b
                ORDER BY id
                """
            ),
            {
                "tenant_a": TENANT_A,
                "tenant_b": TENANT_B,
            },
        ).all()

        # Some tenant-scoped tables are intentionally not represented in
        # this minimal integration fixture.  They may therefore be empty,
        # but an OR predicate must never reveal a row for tenant B.
        assert all(row.tenant_id == TENANT_A for row in rows)


# ---------------------------------------------------------------------------
# Tenant context switching
# ---------------------------------------------------------------------------


def test_switching_tenant_context_changes_visible_rows(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    """
    A connection can change tenant context explicitly, but each context must
    only expose that tenant's data.
    """
    del seeded_tenants

    with app_engine.connect() as conn:
        _set_tenant(conn, TENANT_A)

        tenant_a_rows = conn.execute(
            text(
                """
                SELECT id
                FROM menu_items
                ORDER BY id
                """
            )
        ).scalars().all()

        assert tenant_a_rows == [MENU_ITEM_A]

        _set_tenant(conn, TENANT_B)

        tenant_b_rows = conn.execute(
            text(
                """
                SELECT id
                FROM menu_items
                ORDER BY id
                """
            )
        ).scalars().all()

        assert tenant_b_rows == [MENU_ITEM_B]


def test_fresh_connection_has_no_tenant_context(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    """
    A fresh application connection must not inherit a tenant context from
    another connection.
    """
    del seeded_tenants

    with app_engine.connect() as conn_a:
        _set_tenant(conn_a, TENANT_A)

        assert _count_rows(conn_a, "menu_items") == 1

    with app_engine.connect() as conn_b:
        assert _count_rows(conn_b, "menu_items") == 0


# ---------------------------------------------------------------------------
# SELECT cross-tenant attacks
# ---------------------------------------------------------------------------


def test_tenant_a_cannot_select_tenant_b_by_id(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    del seeded_tenants

    with app_engine.connect() as conn:
        _set_tenant(conn, TENANT_A)

        row = conn.execute(
            text(
                """
                SELECT id
                FROM menu_items
                WHERE id = :id
                """
            ),
            {"id": MENU_ITEM_B},
        ).scalar_one_or_none()

        assert row is None


def test_tenant_b_cannot_select_tenant_a_by_id(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    del seeded_tenants

    with app_engine.connect() as conn:
        _set_tenant(conn, TENANT_B)

        row = conn.execute(
            text(
                """
                SELECT id
                FROM menu_items
                WHERE id = :id
                """
            ),
            {"id": MENU_ITEM_A},
        ).scalar_one_or_none()

        assert row is None


# ---------------------------------------------------------------------------
# INSERT isolation
# ---------------------------------------------------------------------------


def test_tenant_a_cannot_insert_row_for_tenant_b(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    del seeded_tenants

    with app_engine.begin() as conn:
        _set_tenant(conn, TENANT_A)

        with pytest.raises(Exception):
            conn.execute(
                text(
                    """
                    INSERT INTO suppliers (
                        id,
                        tenant_id,
                        name
                    )
                    VALUES (
                        :id,
                        :tenant_id,
                        :name
                    )
                    """
                ),
                {
                    "id": "rls-cross-tenant-supplier",
                    "tenant_id": TENANT_B,
                    "name": "Cross Tenant Supplier",
                },
            )


def test_tenant_b_cannot_insert_row_for_tenant_a(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    del seeded_tenants

    with app_engine.begin() as conn:
        _set_tenant(conn, TENANT_B)

        with pytest.raises(Exception):
            conn.execute(
                text(
                    """
                    INSERT INTO suppliers (
                        id,
                        tenant_id,
                        name
                    )
                    VALUES (
                        :id,
                        :tenant_id,
                        :name
                    )
                    """
                ),
                {
                    "id": "rls-cross-tenant-supplier-b",
                    "tenant_id": TENANT_A,
                    "name": "Cross Tenant Supplier",
                },
            )


# ---------------------------------------------------------------------------
# UPDATE isolation
# ---------------------------------------------------------------------------


def test_tenant_a_cannot_update_tenant_b_row(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    del seeded_tenants

    with app_engine.begin() as conn:
        _set_tenant(conn, TENANT_A)

        result = conn.execute(
            text(
                """
                UPDATE suppliers
                SET name = 'ATTACKED'
                WHERE id = :id
                """
            ),
            {"id": SUPPLIER_B},
        )

        assert result.rowcount == 0


def test_tenant_b_cannot_update_tenant_a_row(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    del seeded_tenants

    with app_engine.begin() as conn:
        _set_tenant(conn, TENANT_B)

        result = conn.execute(
            text(
                """
                UPDATE suppliers
                SET name = 'ATTACKED'
                WHERE id = :id
                """
            ),
            {"id": SUPPLIER_A},
        )

        assert result.rowcount == 0


# ---------------------------------------------------------------------------
# DELETE isolation
# ---------------------------------------------------------------------------


def test_tenant_a_cannot_delete_tenant_b_row(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    del seeded_tenants

    with app_engine.begin() as conn:
        _set_tenant(conn, TENANT_A)

        result = conn.execute(
            text(
                """
                DELETE FROM suppliers
                WHERE id = :id
                """
            ),
            {"id": SUPPLIER_B},
        )

        assert result.rowcount == 0


def test_tenant_b_cannot_delete_tenant_a_row(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    del seeded_tenants

    with app_engine.begin() as conn:
        _set_tenant(conn, TENANT_B)

        result = conn.execute(
            text(
                """
                DELETE FROM suppliers
                WHERE id = :id
                """
            ),
            {"id": SUPPLIER_A},
        )

        assert result.rowcount == 0


# ---------------------------------------------------------------------------
# Tenant identity protection
# ---------------------------------------------------------------------------


def test_tenant_a_cannot_read_tenant_b_record_from_tenants_table(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    del seeded_tenants

    with app_engine.connect() as conn:
        _set_tenant(conn, TENANT_A)

        rows = conn.execute(
            text(
                """
                SELECT id, name
                FROM tenants
                WHERE id = :tenant_id
                """
            ),
            {"tenant_id": TENANT_B},
        ).all()

        assert rows == []


def test_tenant_b_cannot_read_tenant_a_record_from_tenants_table(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    del seeded_tenants

    with app_engine.connect() as conn:
        _set_tenant(conn, TENANT_B)

        rows = conn.execute(
            text(
                """
                SELECT id, name
                FROM tenants
                WHERE id = :tenant_id
                """
            ),
            {"tenant_id": TENANT_A},
        ).all()

        assert rows == []


# ---------------------------------------------------------------------------
# Structural RLS verification
# ---------------------------------------------------------------------------


def test_all_expected_tables_have_rls_enabled(
    admin_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    """
    Verify that every table expected by Component 5 actually has RLS
    enabled.
    """
    del seeded_tenants

    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    c.relname AS table_name,
                    c.relrowsecurity AS rls_enabled,
                    c.relforcerowsecurity AS force_rls
                FROM pg_class c
                JOIN pg_namespace n
                  ON n.oid = c.relnamespace
                WHERE n.nspname = 'public'
                  AND c.relname = ANY(:table_names)
                ORDER BY c.relname
                """
            ),
            {"table_names": list(ALL_RLS_TABLES)},
        ).mappings().all()

        result = {
            row["table_name"]: row
            for row in rows
        }

        for table_name in ALL_RLS_TABLES:
            assert table_name in result, (
                f"Expected RLS table {table_name!r} was not found"
            )

            assert result[table_name]["rls_enabled"] is True, (
                f"RLS is not enabled on {table_name!r}"
            )

            assert result[table_name]["force_rls"] is True, (
                f"FORCE ROW LEVEL SECURITY is not enabled on {table_name!r}"
            )


def test_rls_policies_exist_for_all_expected_tables(
    admin_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    """
    Verify that every expected table has a tenant isolation policy.
    """
    del seeded_tenants

    with admin_engine.connect() as conn:
        rows = conn.execute(
            text(
                """
                SELECT
                    tablename,
                    policyname,
                    permissive,
                    cmd
                FROM pg_policies
                WHERE schemaname = 'public'
                  AND tablename = ANY(:table_names)
                ORDER BY tablename, policyname
                """
            ),
            {"table_names": list(ALL_RLS_TABLES)},
        ).mappings().all()

        policy_tables = {
            row["tablename"]
            for row in rows
            if row["policyname"] == "tenant_isolation"
        }

        for table_name in ALL_RLS_TABLES:
            assert table_name in policy_tables, (
                f"tenant_isolation policy missing from {table_name!r}"
            )


# ---------------------------------------------------------------------------
# Role privilege verification
# ---------------------------------------------------------------------------


def test_test_role_is_not_superuser_or_bypassrls(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    """
    The role used by the security assertions (app_user) must not be able
    to bypass RLS through PostgreSQL role attributes.
    """
    del seeded_tenants

    with app_engine.connect() as conn:
        result = conn.execute(
            text(
                """
                SELECT
                    rolname,
                    rolsuper,
                    rolbypassrls
                FROM pg_roles
                WHERE rolname = current_user
                """
            )
        ).mappings().one()

        assert result["rolsuper"] is False
        assert result["rolbypassrls"] is False


# ---------------------------------------------------------------------------
# Connection/session isolation
# ---------------------------------------------------------------------------


def test_tenant_context_is_connection_local(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    """
    Tenant context set on one database connection must not leak to another
    independent connection.
    """
    del seeded_tenants

    with app_engine.connect() as conn_a:
        _set_tenant(conn_a, TENANT_A)

        assert _count_rows(conn_a, "menu_items") == 1

        with app_engine.connect() as conn_b:
            assert _count_rows(conn_b, "menu_items") == 0


def test_no_context_after_connection_reuse(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    """
    A connection returned to the SQLAlchemy pool must not accidentally cause
    a tenant context to become globally shared.

    The explicit reset here mirrors what production request/session handling
    should do when a pooled connection is reused.
    """
    del seeded_tenants

    with app_engine.connect() as conn:
        _set_tenant(conn, TENANT_A)
        assert _count_rows(conn, "menu_items") == 1

        _set_tenant(conn, None)
        assert _count_rows(conn, "menu_items") == 0


# ---------------------------------------------------------------------------
# Data-integrity / tenant-column enforcement
# ---------------------------------------------------------------------------


def test_tenant_a_insert_with_own_tenant_succeeds(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    """
    RLS must not prevent legitimate writes for the active tenant.
    """
    del seeded_tenants

    test_id = "rls-legitimate-supplier-a"

    try:
        with app_engine.begin() as conn:
            _set_tenant(conn, TENANT_A)

            result = conn.execute(
                text(
                    """
                    INSERT INTO suppliers (
                        id,
                        tenant_id,
                        name
                    )
                    VALUES (
                        :id,
                        :tenant_id,
                        :name
                    )
                    """
                ),
                {
                    "id": test_id,
                    "tenant_id": TENANT_A,
                    "name": "Legitimate Supplier A",
                },
            )

            assert result.rowcount == 1

            visible = conn.execute(
                text(
                    """
                    SELECT id
                    FROM suppliers
                    WHERE id = :id
                    """
                ),
                {"id": test_id},
            ).scalar_one()

            assert visible == test_id
    finally:
        with app_engine.begin() as conn:
            _set_tenant(conn, TENANT_A)

            conn.execute(
                text(
                    """
                    DELETE FROM suppliers
                    WHERE id = :id
                    """
                ),
                {"id": test_id},
            )


def test_tenant_b_insert_with_own_tenant_succeeds(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
) -> None:
    """
    RLS must allow legitimate writes for Tenant B.
    """
    del seeded_tenants

    test_id = "rls-legitimate-supplier-b"

    try:
        with app_engine.begin() as conn:
            _set_tenant(conn, TENANT_B)

            result = conn.execute(
                text(
                    """
                    INSERT INTO suppliers (
                        id,
                        tenant_id,
                        name
                    )
                    VALUES (
                        :id,
                        :tenant_id,
                        :name
                    )
                    """
                ),
                {
                    "id": test_id,
                    "tenant_id": TENANT_B,
                    "name": "Legitimate Supplier B",
                },
            )

            assert result.rowcount == 1

            visible = conn.execute(
                text(
                    """
                    SELECT id
                    FROM suppliers
                    WHERE id = :id
                    """
                ),
                {"id": test_id},
            ).scalar_one()

            assert visible == test_id
    finally:
        with app_engine.begin() as conn:
            _set_tenant(conn, TENANT_B)

            conn.execute(
                text(
                    """
                    DELETE FROM suppliers
                    WHERE id = :id
                    """
                ),
                {"id": test_id},
            )


# ---------------------------------------------------------------------------
# Final broad isolation check
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tenant_id", [TENANT_A, TENANT_B])
def test_every_tenant_scoped_table_contains_only_current_tenant(
    app_engine: Engine,
    seeded_tenants: dict[str, str],
    tenant_id: str,
) -> None:
    """
    Broad invariant:

        Every row returned from every tenant-scoped table must belong to
        the currently active tenant.

    This catches accidental omissions where a policy exists but its
    predicate is incorrect.
    """
    del seeded_tenants

    with app_engine.connect() as conn:
        _set_tenant(conn, tenant_id)

        for table_name in TENANT_SCOPED_TABLES:
            rows = conn.execute(
                text(
                    f"""
                    SELECT DISTINCT tenant_id
                    FROM {table_name}
                    """
                )
            ).scalars().all()

            assert set(rows) <= {tenant_id}, (
                f"Tenant isolation violation in {table_name!r}: "
                f"visible tenants={set(rows)!r}, "
                f"current tenant={tenant_id!r}"
            )

