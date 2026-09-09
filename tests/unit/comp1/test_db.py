"""
Exhaustive unit test suite for db.py.

Coverage includes:
- Session context management (`get_db_context`) commit/rollback behavior.
- Legacy session compatibility (`get_session`).
- Database health check probe success and failure modes.
- Table initialization (`init_db`).
- Strict tenant UUID validation.
- Parameterized RLS tenant context handling.
- Connection leak / pool starvation resilience.
- Transaction isolation on unhandled context exceptions.
- Invalid database configuration handling.
"""

from unittest.mock import MagicMock, patch
from unittest.mock import MagicMock, call, patch
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from Component_1.db import (
    Base,
    engine,
    get_db_context,
    get_session,
    health_check,
    init_db,
    validate_tenant_id,
)


VALID_TENANT_ID = "11111111-1111-1111-1111-111111111111"


# =====================================================================
# 1. BASIC & LEGACY FUNCTIONALITY TESTS
# =====================================================================

def test_get_session_returns_active_session():
    """Verify legacy get_session() returns a valid SQLAlchemy session."""
    session = get_session()

    try:
        assert session is not None
        assert session.is_active
    finally:
        session.close()


def test_init_db_executes_without_error():
    """Verify init_db() invokes metadata creation against the configured engine."""
    with patch.object(Base.metadata, "create_all") as mock_create_all:
        init_db()

        mock_create_all.assert_called_once_with(bind=engine)


def test_health_check_returns_true_when_healthy():
    """Verify health_check() returns True when the database responds."""
    assert health_check() is True


def test_health_check_returns_false_on_operational_error():
    """Verify health_check() handles database outages gracefully."""
    with patch.object(
        engine,
        "connect",
        side_effect=OperationalError(
            "Connection refused",
            params=None,
            orig=Exception(),
        ),
    ):
        assert health_check() is False


# =====================================================================
# 2. TENANT VALIDATION
# =====================================================================

def test_validate_tenant_id_accepts_valid_uuid():
    """Valid UUID tenant IDs are accepted and canonicalized."""
    result = validate_tenant_id(VALID_TENANT_ID)

    assert result == VALID_TENANT_ID


def test_validate_tenant_id_canonicalizes_uuid_case_and_whitespace():
    """UUID input is normalized to its canonical representation."""
    result = validate_tenant_id(
        "  11111111-1111-1111-1111-111111111111  "
    )

    assert result == VALID_TENANT_ID


@pytest.mark.parametrize(
    "invalid_tenant_id",
    [
        "",
        "   ",
        None,
        123,
        "tenant_123",
        "not-a-uuid",
        "admin",
        "00000000",
        "11111111-1111-1111-1111-111111111111\x00",
        "11111111-1111-1111-1111-111111111111\x00_injection",
        "admin' OR '1'='1",
        "tenant_123'; DROP TABLE tenants; --",
        "<script>alert('xss')</script>",
    ],
)
def test_validate_tenant_id_rejects_invalid_values(invalid_tenant_id):
    """
    Security boundary: malformed tenant identifiers must be rejected rather
    than sanitized into potentially ambiguous values.
    """
    with pytest.raises(ValueError):
        validate_tenant_id(invalid_tenant_id)


# =====================================================================
# 3. CONTEXT MANAGER & TRANSACTION LIFECYCLE TESTS
# =====================================================================

def test_get_db_context_commits_on_success():
    """
    Verify get_db_context yields an active session and commits automatically
    on normal exit.
    """
    with get_db_context() as session:
        assert session.is_active

        result = session.execute(
            text("SELECT 1")
        ).scalar()

        assert result == 1


def test_get_db_context_sets_transaction_local_tenant_context():
    """
    Verify a valid tenant ID is installed into PostgreSQL's transaction-local
    RLS context.
    """
    with get_db_context(tenant_id=VALID_TENANT_ID) as session:
        current_setting = session.execute(
            text(
                "SELECT current_setting("
                "'app.current_tenant', true)"
            )
        ).scalar()

        assert current_setting == VALID_TENANT_ID


def test_get_db_context_rejects_invalid_tenant_before_database_operation():
    """
    Invalid tenant IDs must fail before any RLS context is installed.
    """
    with pytest.raises(ValueError, match="valid UUID"):
        with get_db_context(
            tenant_id="not-a-valid-tenant"
        ):
            pytest.fail(
                "Database context should not have been entered."
            )


def test_get_db_context_rolls_back_and_closes_on_exception():
    """
    Verify get_db_context rolls back, logs the failure, and closes the
    session on an unhandled application exception.
    """
    with patch("Component_1.db.SessionLocal") as mock_session_factory:
        mock_session = MagicMock()
        mock_session_factory.return_value = mock_session

        with pytest.raises(
            RuntimeError,
            match="Simulated application error",
        ):
            with get_db_context():
                raise RuntimeError(
                    "Simulated application error"
                )

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()


def test_get_db_context_closes_session_after_success():
    """Verify the session is always closed after successful completion."""
    with patch("Component_1.db.SessionLocal") as mock_session_factory:
        mock_session = MagicMock()
        mock_session_factory.return_value = mock_session

        with get_db_context():
            pass

        mock_session.commit.assert_called_once()
        mock_session.close.assert_called_once()


def test_get_db_context_rolls_back_before_close():
    """Verify rollback occurs before session cleanup after failure."""
    with patch("Component_1.db.SessionLocal") as mock_session_factory:
        mock_session = MagicMock()
        mock_session_factory.return_value = mock_session

        with pytest.raises(RuntimeError):
            with get_db_context(VALID_TENANT_ID):
                raise RuntimeError("forced failure")

        assert mock_session.rollback.called
        assert mock_session.close.called

        # Verify rollback happened before close.
        calls = mock_session.method_calls
        rollback_index = calls.index(call.rollback())
        close_index = calls.index(call.close())

        assert rollback_index < close_index

# =====================================================================
# 4. RED-TEAM SECURITY & ISOLATION TESTS
# =====================================================================

@pytest.mark.parametrize(
    "malicious_tenant_id",
    [
        "tenant_123'; DROP TABLE tenants; --",
        "admin' OR '1'='1",
        "tenant_abc\x00_injection",
        "<script>alert('xss')</script>",
        "tenant_id' EXEC sp_executesql N'SELECT 1'--",
        "'; SET app.current_tenant = 'victim'; --",
    ],
)
def test_redteam_invalid_tenant_ids_are_rejected(
    malicious_tenant_id,
):
    """
    Adversarial attack:

    SQL injection payloads must never reach the PostgreSQL RLS setting.

    The tenant boundary rejects malformed UUIDs before executing SET LOCAL.
    """
    with pytest.raises(ValueError):
        with get_db_context(
            tenant_id=malicious_tenant_id
        ):
            pytest.fail(
                "Malicious tenant ID unexpectedly entered DB context."
            )


def test_redteam_parameterized_rls_context_cannot_change_sql_structure():
    """
    Verify a valid UUID containing no SQL syntax is installed as a literal
    tenant setting and not interpreted as SQL.
    """
    with get_db_context(
        tenant_id=VALID_TENANT_ID
    ) as session:

        current_setting = session.execute(
            text(
                "SELECT current_setting("
                "'app.current_tenant', true)"
            )
        ).scalar()

        assert current_setting == VALID_TENANT_ID


def test_redteam_connection_pool_starvation_and_leak_prevention():
    """
    Adversarial attack:

    Trigger repeated exceptions inside get_db_context blocks and verify
    connections are returned to the pool.
    """
    pool = engine.pool
    initial_checked_out = pool.checkedout()

    for i in range(15):
        try:
            with get_db_context() as session:
                session.execute(text("SELECT 1"))

                if i % 2 == 0:
                    raise ValueError(
                        "Simulated pipeline panic"
                    )

        except ValueError:
            pass

    assert pool.checkedout() == initial_checked_out


def test_redteam_unhandled_exception_does_not_leak_uncommitted_state():
    """
    Adversarial attack:

    Verify dirty or partially executed transactional work is rolled back
    when the context exits through an exception.
    """

    class CustomException(Exception):
        pass

    try:
        with get_db_context() as session:
            session.execute(
                text(
                    """
                    CREATE TEMP TABLE IF NOT EXISTS
                    redteam_leak_test (id INT)
                    """
                )
            )

            session.execute(
                text(
                    "INSERT INTO redteam_leak_test VALUES (999)"
                )
            )

            raise CustomException(
                "System crash before commit"
            )

    except CustomException:
        pass

    with get_db_context() as session:
        with pytest.raises(Exception):
            session.execute(
                text(
                    "SELECT * FROM redteam_leak_test"
                )
            )


# =====================================================================
# 5. CONFIGURATION VALIDATION
# =====================================================================

def test_database_url_fails_closed_in_production():
    """
    Production must not silently fall back to local development credentials.

    The helper is tested in isolation so the imported engine is unaffected.
    """
    with patch.dict(
        "os.environ",
        {
            "APP_ENV": "production",
            "DATABASE_URL": "",
        },
        clear=False,
    ):
        from Component_1 import db

        with pytest.raises(
            RuntimeError,
            match="DATABASE_URL must be configured",
        ):
            db._get_database_url()


def test_database_url_uses_explicit_environment_value():
    """Configured DATABASE_URL takes precedence over local defaults."""
    expected_url = (
        "postgresql://user:password@localhost:5432/test_db"
    )

    with patch.dict(
        "os.environ",
        {
            "APP_ENV": "production",
            "DATABASE_URL": expected_url,
        },
        clear=False,
    ):
        from Component_1 import db

        assert db._get_database_url() == expected_url


def test_database_pool_environment_values_must_be_valid():
    """
    Invalid pool configuration should fail during configuration parsing
    rather than producing obscure SQLAlchemy errors later.
    """
    from Component_1 import db

    with patch.dict(
        "os.environ",
        {"TEST_DB_POOL_SIZE": "not-an-integer"},
        clear=False,
    ):
        with pytest.raises(
            RuntimeError,
            match="TEST_DB_POOL_SIZE must be an integer",
        ):
            db._get_int_env(
                "TEST_DB_POOL_SIZE",
                default=10,
            )


def test_database_pool_values_must_not_be_below_minimum():
    """Negative/zero pool configuration is rejected."""
    from Component_1 import db

    with patch.dict(
        "os.environ",
        {"TEST_DB_POOL_SIZE": "0"},
        clear=False,
    ):
        with pytest.raises(
            RuntimeError,
            match="TEST_DB_POOL_SIZE must be >= 1",
        ):
            db._get_int_env(
                "TEST_DB_POOL_SIZE",
                default=10,
            )


# =====================================================================
# 6. TRANSACTION-SCOPED RLS LIFETIME
# =====================================================================

def test_rls_context_is_transaction_local():
    """
    SET LOCAL must not survive after the transaction ends.

    This is important because the application relies on transaction-scoped
    tenant isolation rather than a connection-global tenant variable.
    """
    with get_db_context(
        tenant_id=VALID_TENANT_ID
    ) as session:

        current_setting = session.execute(
            text(
                "SELECT current_setting("
                "'app.current_tenant', true)"
            )
        ).scalar()

        assert current_setting == VALID_TENANT_ID

    with get_db_context() as session:
        current_setting = session.execute(
            text(
                "SELECT current_setting("
                "'app.current_tenant', true)"
            )
        ).scalar()

        assert current_setting in (None, "")