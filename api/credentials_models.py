"""
API-key credential storage.

IMPORTANT -- this table is deliberately NOT added to
Component_5/enable_rls.py's TENANT_SCOPED_TABLES. RLS policies filter rows
by `app.current_tenant`, but that setting can't exist yet at the moment
we're looking up WHICH tenant an API key belongs to -- that's exactly what
this table's lookup determines. Querying it therefore uses a plain,
non-tenant-scoped session (Component_1.db.get_session()), never
get_db_context(tenant_id). Every query against this table must filter by
key_hash (an exact, narrow, indexed lookup) -- never list/scan it.

Only the HASH of the key is stored, salted with a server-side pepper
(API_KEY_PEPPER, from Secrets Manager in deployment). The raw key is shown
to the caller exactly once, at issuance, and never persisted.
"""

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
)

from Component_1.db import Base
from Component_1.models import _now, _uuid


class ApiCredential(Base):
    __tablename__ = "api_credentials"

    id = Column(String, primary_key=True, default=_uuid)

    tenant_id = Column(
        String,
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # SHA-256(pepper + raw_key), hex-encoded. Unique so a lookup is a single
    # indexed equality match, never a scan.
    key_hash = Column(String(64), nullable=False, unique=True, index=True)

    # A short, non-secret prefix of the raw key (e.g. first 8 chars) shown
    # back to the tenant so they can identify which key is which in an
    # admin UI without ever re-displaying the full secret.
    key_prefix = Column(String(16), nullable=False)

    active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime(timezone=True), default=_now, nullable=False)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "length(key_hash) = 64",
            name="ck_api_credential_hash_length",
        ),
        Index(
            "idx_api_credentials_tenant_active",
            "tenant_id",
            "active",
        ),
    )