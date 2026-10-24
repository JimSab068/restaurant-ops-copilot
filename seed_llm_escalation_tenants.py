"""Add five synthetic tenants with progressively severe margin opportunities.

Run this only as a one-off ECS task with the bootstrap task's restricted
database and Secrets Manager configuration. Unlike bootstrap, it does not
alter schema, RLS policies, existing tenant data, or existing API keys.
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from typing import Any

import boto3

_seed_database_url = os.environ.get("SEED_DATABASE_URL")
if not _seed_database_url:
    raise RuntimeError("SEED_DATABASE_URL must be set.")
os.environ["DATABASE_URL"] = _seed_database_url

from Component_1.db import get_session
from Component_1.models import Ingredient, MenuItem, Tenant, _now, _uuid
from api.auth import hash_api_key
from api.credentials_models import ApiCredential
from seed import IngredientConfig, MenuItemConfig, TenantConfig, _seed_tenant


FIXTURES_SECRET_ARN = os.environ.get("ACCEPTANCE_FIXTURES_SECRET_ARN")
if not FIXTURES_SECRET_ARN:
    raise RuntimeError("ACCEPTANCE_FIXTURES_SECRET_ARN must be set.")

ESCALATION_TENANTS = tuple(
    TenantConfig(
        name=f"LLM Escalation {level} - {severity}",
        suppliers=("Synthetic Supplier",),
        ingredients=(
            IngredientConfig("Premium Base Ingredient", "kg", 10.0, 50.0),
            IngredientConfig("Second Base Ingredient", "kg", 10.0, 50.0),
        ),
        menu_items=(
            MenuItemConfig("Margin Stress Special", price),
        ),
    )
    for level, severity, price in (
        (1, "Mild", 4.25),
        (2, "Elevated", 4.00),
        (3, "High", 3.75),
        (4, "Severe", 3.50),
        (5, "Critical", 3.25),
    )
)


def _read_fixtures(secrets_client: Any) -> dict[str, dict[str, Any]]:
    response = secrets_client.get_secret_value(SecretId=FIXTURES_SECRET_ARN)
    secret_string = response.get("SecretString")
    if not isinstance(secret_string, str):
        raise RuntimeError("Acceptance fixtures secret has no SecretString.")
    fixtures = json.loads(secret_string)
    if not isinstance(fixtures, dict):
        raise RuntimeError("Acceptance fixtures secret must contain a JSON object.")
    if any(not isinstance(value, dict) for value in fixtures.values()):
        raise RuntimeError("Acceptance fixtures must map tenant IDs to objects.")
    return fixtures


def add_escalation_tenants() -> int:
    region = os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-east-1"
    )
    secrets_client = boto3.client("secretsmanager", region_name=region)
    fixtures = _read_fixtures(secrets_client)
    session = get_session()
    created_count = 0

    try:
        for config in ESCALATION_TENANTS:
            tenant = (
                session.query(Tenant)
                .filter(Tenant.name == config.name)
                .one_or_none()
            )
            if tenant is None:
                tenant_id, _ = _seed_tenant(session, config)
                tenant = session.query(Tenant).filter(Tenant.id == tenant_id).one()
                created_count += 1

            tenant_id = str(tenant.id)
            menu_item = (
                session.query(MenuItem)
                .filter(
                    MenuItem.tenant_id == tenant_id,
                    MenuItem.active.is_(True),
                )
                .order_by(MenuItem.id)
                .first()
            )
            ingredient = (
                session.query(Ingredient)
                .filter(Ingredient.tenant_id == tenant_id)
                .order_by(Ingredient.id)
                .first()
            )
            if menu_item is None or ingredient is None:
                raise RuntimeError(
                    f"Escalation tenant {config.name!r} lacks fixture entities."
                )

            existing_fixture = fixtures.get(tenant_id, {})
            raw_key = existing_fixture.get("api_key")
            credential = None
            if isinstance(raw_key, str) and raw_key:
                credential = (
                    session.query(ApiCredential)
                    .filter(
                        ApiCredential.tenant_id == tenant_id,
                        ApiCredential.key_hash == hash_api_key(raw_key),
                        ApiCredential.active.is_(True),
                    )
                    .one_or_none()
                )

            if credential is None:
                raw_key = secrets.token_urlsafe(32)
                now = _now()
                session.query(ApiCredential).filter(
                    ApiCredential.tenant_id == tenant_id,
                    ApiCredential.active.is_(True),
                ).update(
                    {
                        ApiCredential.active: False,
                        ApiCredential.revoked_at: now,
                    },
                    synchronize_session=False,
                )
                session.add(
                    ApiCredential(
                        id=_uuid(),
                        tenant_id=tenant_id,
                        key_hash=hash_api_key(raw_key),
                        key_prefix=raw_key[:8],
                        active=True,
                        created_at=now,
                    )
                )

            fixtures[tenant_id] = {
                "api_key": raw_key,
                "menu_item_id": str(menu_item.id),
                "menu_item_price": float(menu_item.current_price),
                "ingredient_id": str(ingredient.id),
                "ingredient_price": float(ingredient.current_price),
            }

        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    secrets_client.put_secret_value(
        SecretId=FIXTURES_SECRET_ARN,
        SecretString=json.dumps(fixtures, indent=2) + "\n",
    )
    return created_count


def main() -> int:
    if "--confirm" not in sys.argv[1:]:
        print(
            "This adds five synthetic tenants and updates the acceptance "
            "fixtures secret. Re-run with --confirm to proceed.",
            file=sys.stderr,
        )
        return 2

    created_count = add_escalation_tenants()
    print(
        f"Added {created_count} new LLM escalation tenant(s); "
        f"fixtures now contain the existing tenants plus five scenarios."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
