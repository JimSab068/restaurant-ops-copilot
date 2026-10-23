"""
Issue a new API key for a tenant.

The raw key is shown ONCE, here, and never stored anywhere. Only its
hash (via api.auth.hash_api_key, salted with API_KEY_PEPPER) is
persisted. If the key is lost, issue a new one and revoke the old one --
there is no recovery path by design.

Usage:
    python -m api.generate_api_key <tenant_id>
"""

from __future__ import annotations

import secrets
import sys

from Component_1.db import get_session
from Component_1.models import _now, _uuid

from .auth import hash_api_key
from .credentials_models import ApiCredential


def issue_key(tenant_id: str) -> str:
    raw_key = secrets.token_urlsafe(32)
    key_hash = hash_api_key(raw_key)
    key_prefix = raw_key[:8]

    session = get_session()
    try:
        credential = ApiCredential(
            id=_uuid(),
            tenant_id=tenant_id,
            key_hash=key_hash,
            key_prefix=key_prefix,
            active=True,
            created_at=_now(),
        )
        session.add(credential)
        session.commit()
    finally:
        session.close()

    return raw_key


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python -m api.generate_api_key <tenant_id>", file=sys.stderr)
        return 1

    tenant_id = sys.argv[1]
    raw_key = issue_key(tenant_id)

    print(f"Tenant:  {tenant_id}")
    print(f"API key: {raw_key}")
    print("Store this now -- it will not be shown again.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())