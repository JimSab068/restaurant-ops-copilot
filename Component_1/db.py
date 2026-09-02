"""
Database connection and session management.

The application uses PostgreSQL in both local development and production so
database semantics remain consistent across environments.

Local development:
    export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/ops_copilot"

Production:
    DATABASE_URL should be supplied through the deployment environment or
    secret manager. The application does not silently fall back to local
    credentials when running in production.

The database layer is responsible for:

- SQLAlchemy engine/pool configuration.
- Session lifecycle management.
- Transaction-safe tenant RLS context.
- Database health checks.
- Tenant identifier validation.
"""

from contextlib import contextmanager
import logging
import os
from typing import Generator, Optional
from uuid import UUID

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, declarative_base, sessionmaker


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _get_database_url() -> str:
    """
    Return the configured database URL.

    Local development may use the explicit localhost fallback. Production
    deployments must provide DATABASE_URL.
    """
    database_url = os.environ.get("DATABASE_URL")

    if database_url:
        return database_url

    environment = os.environ.get("APP_ENV", "development").lower()

    if environment in {"production", "prod"}:
        raise RuntimeError(
            "DATABASE_URL must be configured in production."
        )

    return "postgresql://postgres:postgres@localhost:5432/ops_copilot"


DATABASE_URL = _get_database_url()


def _get_int_env(name: str, default: int, minimum: int = 1) -> int:
    """Read a positive integer environment variable safely."""
    raw_value = os.environ.get(name)

    if raw_value is None:
        return default

    try:
        value = int(raw_value)
    except ValueError as exc:
        raise RuntimeError(
            f"{name} must be an integer."
        ) from exc

    if value < minimum:
        raise RuntimeError(
            f"{name} must be >= {minimum}."
        )

    return value


DB_POOL_SIZE = _get_int_env("DB_POOL_SIZE", 10)
DB_MAX_OVERFLOW = _get_int_env("DB_MAX_OVERFLOW", 20, minimum=0)
DB_POOL_TIMEOUT = _get_int_env("DB_POOL_TIMEOUT", 30)
DB_POOL_RECYCLE = _get_int_env("DB_POOL_RECYCLE", 1800)


# ---------------------------------------------------------------------------
# Engine / session factory
# ---------------------------------------------------------------------------

engine: Engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_size=DB_POOL_SIZE,
    max_overflow=DB_MAX_OVERFLOW,
    pool_timeout=DB_POOL_TIMEOUT,
    pool_recycle=DB_POOL_RECYCLE,
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(
    bind=engine,
    autoflush=False,
    autocommit=False,
)

Base = declarative_base()


# ---------------------------------------------------------------------------
# Tenant validation
# ---------------------------------------------------------------------------

def validate_tenant_id(tenant_id: str) -> str:
    """
    Validate and canonicalize a tenant UUID.

    Tenant identifiers are security-sensitive because they determine the RLS
    context. We therefore reject malformed identifiers instead of attempting
    to sanitize them into something usable.
    """
    if not isinstance(tenant_id, str):
        raise ValueError("tenant_id must be a string.")

    tenant_id = tenant_id.strip()

    if not tenant_id:
        raise ValueError("tenant_id must not be empty.")

    if "\x00" in tenant_id:
        raise ValueError("tenant_id contains an invalid null byte.")

    try:
        return str(UUID(tenant_id))
    except ValueError as exc:
        raise ValueError(
            "tenant_id must be a valid UUID."
        ) from exc


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def get_session() -> Session:
    """
    Return a raw SQLAlchemy session.

    This is retained for backwards compatibility. New code should prefer
    get_db_context() so transaction and cleanup behavior are explicit.
    """
    return SessionLocal()


@contextmanager
def get_db_context(
    tenant_id: Optional[str] = None,
) -> Generator[Session, None, None]:
    """
    Provide a transaction-scoped database session.

    When tenant_id is supplied, PostgreSQL's transaction-local RLS context is
    established using SET LOCAL. The context therefore remains active for the
    complete transaction and disappears automatically after commit/rollback.

    On successful completion:
        commit

    On failure:
        rollback + re-raise

    Always:
        close the session
    """
    session = SessionLocal()

    try:
        if tenant_id is not None:
            clean_tenant_id = validate_tenant_id(tenant_id)

            session.execute(
                text("SET LOCAL app.current_tenant = :tenant_id"),
                {"tenant_id": clean_tenant_id},
            )

        yield session
        session.commit()

    except Exception:
        session.rollback()
        logger.exception("Database transaction failed.")
        raise

    finally:
        session.close()


# ---------------------------------------------------------------------------
# Database initialization / health
# ---------------------------------------------------------------------------

def init_db() -> None:
    """
    Create database tables.

    This is useful for local development and tests.

    Production deployments should prefer explicit database migrations rather
    than relying on create_all().
    """
    Base.metadata.create_all(bind=engine)


def health_check() -> bool:
    """
    Check whether the database is reachable and responsive.

    Intended for ECS/Kubernetes readiness or liveness endpoints.
    """
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))

        return True

    except OperationalError:
        logger.exception("Database health check failed.")
        return False