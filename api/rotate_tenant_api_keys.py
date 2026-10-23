"""Rotate API keys for configured synthetic tenants and refresh fixtures.

Usage:
    python -m api.rotate_tenant_api_keys --confirm

SEED_DATABASE_URL must be an administrative database URL that can update
credentials for every configured synthetic tenant. API_KEY_PEPPER must match
the pepper used by the running API.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import tempfile
from pathlib import Path

import boto3
from botocore.exceptions import BotoCoreError

_seed_database_url = os.environ.get("SEED_DATABASE_URL")
if not _seed_database_url:
    raise RuntimeError(
        "SEED_DATABASE_URL must be set to an administrative URL for key rotation."
    )
os.environ["DATABASE_URL"] = _seed_database_url

from Component_1.db import get_session  # noqa: E402
from Component_1.models import Ingredient, MenuItem, Tenant, _now, _uuid  # noqa: E402
from .auth import hash_api_key  # noqa: E402
from .credentials_models import ApiCredential  # noqa: E402
from seed import TENANT_CONFIGS  # noqa: E402


def _tenant_fixtures(session) -> dict[str, dict[str, str | float]]:
    configured_names = [config.name for config in TENANT_CONFIGS]
    tenants_by_name = {
        tenant.name: tenant
        for tenant in session.query(Tenant)
        .filter(Tenant.name.in_(configured_names))
        .all()
    }
    missing_names = [name for name in configured_names if name not in tenants_by_name]
    if missing_names:
        raise RuntimeError(
            "Cannot rotate keys; configured tenants are missing: "
            + ", ".join(missing_names)
        )

    fixtures: dict[str, dict[str, str | float]] = {}
    tenant_ids: list[str] = []
    new_credentials: list[ApiCredential] = []
    now = _now()

    for config in TENANT_CONFIGS:
        tenant = tenants_by_name[config.name]
        tenant_id = str(tenant.id)
        tenant_ids.append(tenant_id)

        menu_item = (
            session.query(MenuItem)
            .filter(MenuItem.tenant_id == tenant_id, MenuItem.active.is_(True))
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
                f"Cannot write fixtures; tenant {config.name!r} needs an active "
                "menu item and an ingredient."
            )

        raw_key = secrets.token_urlsafe(32)
        new_credentials.append(
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

    session.query(ApiCredential).filter(
        ApiCredential.tenant_id.in_(tenant_ids),
        ApiCredential.active.is_(True),
    ).update(
        {
            ApiCredential.active: False,
            ApiCredential.revoked_at: now,
        },
        synchronize_session=False,
    )
    session.add_all(new_credentials)
    return fixtures


def _write_pending_fixtures(path: Path, fixtures: dict[str, dict]) -> Path:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as output:
        json.dump(fixtures, output, indent=2)
        output.write("\n")
        temporary_path = Path(output.name)
    temporary_path.chmod(0o600)
    return temporary_path


def _publish_fixtures_secret(secret_arn: str, fixtures: dict) -> None:
    region = os.environ.get("AWS_REGION") or os.environ.get(
        "AWS_DEFAULT_REGION", "us-east-2"
    )
    try:
        boto3.client("secretsmanager", region_name=region).put_secret_value(
            SecretId=secret_arn,
            SecretString=json.dumps(fixtures, indent=2) + "\n",
        )
    except BotoCoreError as exc:
        raise RuntimeError(
            "Tenant keys were rotated in the database, but the fixture secret "
            "could not be updated. Rerun this command to rotate keys and retry "
            "publishing the matching fixtures."
        ) from exc


def rotate_keys(
    fixtures_path: Path | None = None,
    secret_arn: str | None = None,
) -> int:
    if (fixtures_path is None) == (secret_arn is None):
        raise ValueError("Provide exactly one of fixtures_path or secret_arn.")

    session = get_session()
    pending_path: Path | None = None
    committed = False
    try:
        fixtures = _tenant_fixtures(session)
        if fixtures_path is not None:
            pending_path = _write_pending_fixtures(fixtures_path, fixtures)
        session.commit()
        committed = True
        if fixtures_path is not None and pending_path is not None:
            try:
                os.replace(pending_path, fixtures_path)
            except OSError as exc:
                raise RuntimeError(
                    "Keys were rotated in the database, but the fixtures file "
                    f"could not be replaced. Recover the new keys from {pending_path}."
                ) from exc
            fixtures_path.chmod(0o600)
        elif secret_arn is not None:
            _publish_fixtures_secret(secret_arn, fixtures)
        return len(fixtures)
    except Exception:
        if not committed:
            session.rollback()
            if pending_path is not None and pending_path.exists():
                pending_path.unlink()
        raise
    finally:
        session.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Revoke current keys and issue new keys for every configured "
            "synthetic tenant, replacing tenant_fixtures.json."
        )
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Confirm that all current keys for configured tenants will be revoked.",
    )
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument(
        "--fixtures-file",
        type=Path,
        help="Fixture JSON path to replace.",
    )
    destination.add_argument(
        "--secret-arn",
        help="Secrets Manager secret ARN where the generated fixtures are stored.",
    )
    args = parser.parse_args()

    if not args.confirm:
        parser.error("key rotation is destructive; pass --confirm to proceed")
    if args.fixtures_file is None and args.secret_arn is None:
        args.fixtures_file = Path("tenant_fixtures.json")

    count = rotate_keys(args.fixtures_file, args.secret_arn)
    print(f"Rotated API keys for {count} synthetic tenants.")
    if args.secret_arn:
        print("Published acceptance fixtures to Secrets Manager.")
    else:
        print(f"Updated fixtures file: {args.fixtures_file}")
        print("Existing keys for these tenants are now revoked; protect the fixtures file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
