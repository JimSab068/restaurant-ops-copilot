"""
Exhaustive unit test suite for db.py.

Coverage includes:
- Session context management (`get_db_context`) commit/rollback behavior.
- Legacy session compatibility (`get_session`).
- Database health check probe success and failure modes.
- Table initialization (`init_db`).
- Red-Team Security: Parameterized RLS tenant_id injection resistance.
- Red-Team Security: Session leak prevention and connection pool starvation resilience.
- Red-Team Security: Transaction isolation on unhandled context exceptions.
"""

from unittest.mock import MagicMock, patch
import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from db import (
    Base,
    engine,
    get_db_context,
    get_session,
    health_check,
    init_db,
)


# =====================================================================
# 1. BASIC & LEGACY FUNCTIONALITY TESTS
# =====================================================================

def test_get_session_returns_active_session():
    """Verify legacy `get_session()` getter returns a valid SQLAlchemy session."""
    session = get_session()
    try:
        assert session is not None
        assert session.is_active
    finally:
        session.close()


def test_init_db_executes_without_error():
    """Verify `init_db()` invokes metadata creation against the configured engine."""
    with patch.object(Base.metadata, "create_all") as mock_create_all:
        init_db()
        mock_create_all.assert_called_once_with(bind=engine)


def test_health_check_returns_true_when_healthy():
    """Verify `health_check()` returns True when the database responds to 'SELECT 1'."""
    assert health_check() is True


def test_health_check_returns_false_on_operational_error():
    """Verify `health_check()` handles database outages gracefully without throwing uncaught errors."""
    with patch.object(engine, "connect", side_effect=OperationalError("Connection refused", params=None, orig=Exception())):
        assert health_check() is False


# =====================================================================
# 2. CONTEXT MANAGER & TRANSACTION LIFECYCLE TESTS
# =====================================================================

def test_get_db_context_commits_on_success():
    """Verify `get_db_context` yields an active session and commits automatically on normal exit."""
    with get_db_context() as session:
        assert session.is_active
        # Execute a light query to prove session is usable
        result = session.execute(text("SELECT 1")).scalar()
        assert result == 1


def test_get_db_context_rolls_back_and_closes_on_exception():
    """Verify `get_db_context` executes a rollback, logs error, and closes session on unhandled exceptions."""
    with patch("db.SessionLocal") as mock_session_factory:
        mock_session = MagicMock()
        mock_session_factory.return_value = mock_session

        with pytest.raises(RuntimeError, match="Simulated application error"):
            with get_db_context():
                raise RuntimeError("Simulated application error")

        mock_session.rollback.assert_called_once()
        mock_session.close.assert_called_once()


# =====================================================================
# 3. RED-TEAM SECURITY & ISOLATION TESTS
# =====================================================================

@pytest.mark.parametrize(
    "malicious_tenant_id",
    [
        "tenant_123'; DROP TABLE tenants; --",
        "admin' OR '1'='1",
        "tenant_abc\x00_injection",
        "<script>alert('xss')</script>",
        "tenant_id' EXEC sp_executesql N'SELECT 1'--",
    ],
)
def test_redteam_rls_tenant_context_injection_safety(malicious_tenant_id):
    """
    Adversarial Attack: Inject raw SQL payloads inside the `tenant_id` context parameter.
    Verifies that `SET LOCAL app.current_tenant` uses strict query parameterization
    and null-byte stripping, preventing SQL syntax manipulation or command injection.
    """
    with get_db_context(tenant_id=malicious_tenant_id) as session:
        current_setting = session.execute(
            text("SELECT current_setting('app.current_tenant', true)")
        ).scalar()

        expected_tenant_id = malicious_tenant_id.replace("\x00", "")
        assert current_setting == expected_tenant_id


def test_redteam_connection_pool_starvation_and_leak_prevention():
    """
    Adversarial Attack: Trigger rapid successive exceptions inside `get_db_context` blocks
    to force potential connection leaks and verify the pool does not exhaust.
    """
    pool = engine.pool
    initial_checked_out = pool.checkedout()

    for i in range(15):  # Force multiple crashed contexts
        try:
            with get_db_context() as session:
                session.execute(text("SELECT 1"))
                if i % 2 == 0:
                    raise ValueError("Simulated pipeline panic")
        except ValueError:
            pass

    # Ensure all connections were returned to the pool
    assert pool.checkedout() == initial_checked_out


def test_redteam_unhandled_exception_does_not_leak_uncommitted_state():
    """
    Adversarial Attack: Verify that dirty or partial writes executed inside a failing
    `get_db_context` block are never visible to subsequent database transactions.
    """
    class CustomException(Exception):
        pass

    # Attempt an insert/mutation that raises mid-way
    try:
        with get_db_context() as session:
            session.execute(
                text("CREATE TEMP TABLE IF NOT EXISTS redteam_leak_test (id INT)")
            )
            session.execute(text("INSERT INTO redteam_leak_test VALUES (999)"))
            raise CustomException("System crash before commit")
    except CustomException:
        pass

    # Open a fresh context and verify the temporary state was fully rolled back
    with get_db_context() as session:
        # Querying the uncommitted table should throw a relation-does-not-exist error
        with pytest.raises(Exception):
            session.execute(text("SELECT * FROM redteam_leak_test"))