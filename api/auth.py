"""
API-key authentication.

The one rule this module exists to enforce: tenant identity is derived
ONLY from the authenticated key. Nothing downstream -- not the request
body, not a header, not a queue message -- is ever allowed to name a
different tenant_id than what this module resolves. There is no field
anywhere in the request path a caller can use to impersonate another
tenant, because tenant_id is never read from anything the caller sent.
"""

from __future__ import annotations

import hashlib
import os

from fastapi import Header, HTTPException, status

from Component_1.db import get_session
from Component_1.models import _now
from .credentials_models import ApiCredential

# In deployment this comes from Secrets Manager / Parameter Store, injected
# as an environment variable into the ECS task -- never committed, never
# baked into the image.
API_KEY_PEPPER = os.environ.get("API_KEY_PEPPER", "")

if not API_KEY_PEPPER:
    # Fail loud rather than silently hashing with an empty pepper, which
    # would make key_hash trivially guessable from the raw key alone.
    raise RuntimeError(
        "API_KEY_PEPPER is not set. Refusing to start with an unpeppered "
        "credential hash."
    )


def hash_api_key(raw_key: str) -> str:
    if not isinstance(raw_key, str) or not raw_key:
        raise ValueError("raw_key must be a non-empty string")
    return hashlib.sha256(f"{API_KEY_PEPPER}{raw_key}".encode("utf-8")).hexdigest()


class AuthenticationError(Exception):
    """Raised when a key is missing, malformed, unknown, or revoked."""


def resolve_tenant_from_key(raw_key: str) -> str:
    """
    Look up the tenant_id for a raw API key.

    Uses a plain, non-tenant-scoped session (get_session(), not
    get_db_context) because tenant_id is not known yet -- this IS the
    lookup that determines it. The query is a single exact key_hash
    equality match; nothing here ever scans or lists credentials.
    """
    if not raw_key or not raw_key.strip():
        raise AuthenticationError("missing API key")

    key_hash = hash_api_key(raw_key.strip())

    session = get_session()
    try:
        credential = (
            session.query(ApiCredential)
            .filter(ApiCredential.key_hash == key_hash)
            .one_or_none()
        )

        if credential is None:
            raise AuthenticationError("invalid API key")

        if not credential.active or credential.revoked_at is not None:
            raise AuthenticationError("API key has been revoked")

        credential.last_used_at = _now()
        session.commit()

        return credential.tenant_id
    finally:
        session.close()


async def require_tenant(
    authorization: str | None = Header(default=None),
) -> str:
    """
    FastAPI dependency: extracts `Authorization: Bearer <key>`, resolves
    it to a tenant_id, and returns that tenant_id. Every route that
    touches tenant data must depend on this rather than reading tenant_id
    from anywhere else.
    """
    if authorization is None or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or malformed Authorization header",
        )

    raw_key = authorization.removeprefix("Bearer ").strip()

    try:
        return resolve_tenant_from_key(raw_key)
    except AuthenticationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
        ) from exc