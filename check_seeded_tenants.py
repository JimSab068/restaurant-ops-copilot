"""
Read-only check: what's actually in the database right now, compared
against seed.py's TENANT_CONFIGS.

Run this before deciding whether to run seed.py at all -- it never writes
anything.

Usage:
    python check_seeded_tenants.py
"""
from __future__ import annotations

from Component_1.db import get_session
from Component_1.models import Ingredient, MenuItem, Supplier, Tenant

try:
    from api.credentials_models import ApiCredential
    _HAS_CREDENTIALS_TABLE = True
except ImportError:
    _HAS_CREDENTIALS_TABLE = False

from seed import TENANT_CONFIGS


def main() -> None:
    session = get_session()
    try:
        existing = {t.name: t for t in session.query(Tenant).all()}

        expected_names = {config.name for config in TENANT_CONFIGS}
        present_names = set(existing.keys())

        print(f"Expected tenants (from seed.py): {len(expected_names)}")
        print(f"Tenants currently in the database: {len(present_names)}")
        print()

        missing = expected_names - present_names
        extra = present_names - expected_names
        duplicates = [
            name for name, count in
            {n: list(existing.keys()).count(n) for n in existing}.items()
            if count > 1
        ]

        if missing:
            print(f"MISSING ({len(missing)}) -- seed.py's main() has not created these yet:")
            for name in sorted(missing):
                print(f"  - {name}")
            print()

        if extra:
            print(f"UNEXPECTED ({len(extra)}) -- in the DB but not in TENANT_CONFIGS:")
            for name in sorted(extra):
                print(f"  - {name}")
            print()

        if duplicates:
            print(f"DUPLICATES ({len(duplicates)}) -- same name seeded more than once, likely a re-run with no idempotency guard:")
            for name in duplicates:
                print(f"  - {name}")
            print()

        print("Per-tenant detail:")
        print(f"{'Name':<28} {'Tenant ID':<38} {'MenuItems':>10} {'Ingredients':>12} {'Suppliers':>10} {'API Key?':>9}")
        for config in TENANT_CONFIGS:
            tenant = existing.get(config.name)
            if tenant is None:
                print(f"{config.name:<28} {'-- not seeded --':<38} {'':>10} {'':>12} {'':>10} {'':>9}")
                continue

            menu_count = session.query(MenuItem).filter(MenuItem.tenant_id == tenant.id).count()
            ingredient_count = session.query(Ingredient).filter(Ingredient.tenant_id == tenant.id).count()
            supplier_count = session.query(Supplier).filter(Supplier.tenant_id == tenant.id).count()

            has_key = "?"
            if _HAS_CREDENTIALS_TABLE:
                has_key = "yes" if (
                    session.query(ApiCredential)
                    .filter(ApiCredential.tenant_id == tenant.id, ApiCredential.active.is_(True))
                    .first()
                    is not None
                ) else "no"

            print(f"{config.name:<28} {tenant.id:<38} {menu_count:>10} {ingredient_count:>12} {supplier_count:>10} {has_key:>9}")

        print()
        if missing or duplicates:
            print("Action needed: run the patched seed.py (idempotent) to fill in what's missing.")
            print("Do NOT run the original seed.py again if duplicates were found above -- it will add more.")
        else:
            print("All 10 expected tenants are present, no duplicates. Nothing to seed.")

    finally:
        session.close()


if __name__ == "__main__":
    main()