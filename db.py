"""
Database connection setup.

Runs on PostgreSQL both locally and on AWS, so behavior matches production
from day one (SQLite and Postgres differ subtly on JSON/Boolean/Enum
handling, which isn't worth discovering after deploying).

Local dev:
    export DATABASE_URL="postgresql://postgres:postgres@localhost:5432/ops_copilot"

AWS deploy: point DATABASE_URL at your RDS endpoint instead — no code changes:
    export DATABASE_URL="postgresql://user:pass@your-rds-endpoint:5432/ops_copilot"


    Version 2:
    Upgrades applied:
- Connection pool recycling and pre-pinging to handle AWS RDS connection drops.
- Context manager (`get_db_context`) ensuring sessions close and rollback safely.
- Session-level Tenant RLS setter (`SET LOCAL app.current_tenant`).
- Database health check function for Kubernetes/ECS readiness probes.   
"""
import os
import logging
from contextlib import contextmanager
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from sqlalchemy.exc import OperationalError


logger = logging.getLogger(__name__)


# Fallback string only used for local dev; production must pass DATABASE_URL via secret managers
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/ops_copilot"
)

# Production Pool Configuration
engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_size=int(os.environ.get("DB_POOL_SIZE", 10)),
    max_overflow=int(os.environ.get("DB_MAX_OVERFLOW", 20)),
    pool_timeout=30,
    pool_recycle=1800,  # Recycle connections every 30 mins to prevent stale AWS RDS drops
    pool_pre_ping=True,  # Test connection health before handing it out from pool
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def get_session() -> Session:
    """Legacy session getter maintained for backwards compatibility."""
    return SessionLocal()


@contextmanager
def get_db_context(tenant_id: str = None) -> Generator[Session, None, None]:
    """
    Context manager for database operations. Guarantees session closing and
    automatic rollback on unhandled exceptions. Optionally sets session-level RLS context.
    """
    session = SessionLocal()
    try:
        if tenant_id:
            # Strip null bytes to prevent DB driver errors on malicious input
            clean_tenant_id = tenant_id.replace("\x00", "")
            session.execute(
                text("SET LOCAL app.current_tenant = :tenant_id"),
                {"tenant_id": clean_tenant_id},
            )
        yield session
        session.commit()
    except Exception as e:
        session.rollback()
        logger.error(f"Database session error: {e}", exc_info=True)
        raise
    finally:
        session.close()


def init_db() -> None:
    """Creates all tables. Should be called during deployment migrations."""
    Base.metadata.create_all(bind=engine)


def health_check() -> bool:
    """
    Liveness/readiness probe utility for ECS/Kubernetes health endpoints.
    Returns True if database connection is alive and responding.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError as e:
        logger.critical(f"Database health check failed: {e}")
        return False