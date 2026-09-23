# Component_5/bootstrap_ops_copilot.py
"""
One-off schema bootstrap for the ops_copilot database (used by the RLS
integration tests). Run manually, once, whenever a new table is added
to any Component's models — this DB is not managed by the app's normal
test fixtures (see Component_1/conftest.py, which only touches
ops_copilot_test).

Usage:
    python -m Component_5.bootstrap_ops_copilot
"""

import os

os.environ["DATABASE_URL"] = os.environ.get(
    "RLS_ADMIN_DATABASE_URL",
    "postgresql://rls_admin:1234@localhost:5432/ops_copilot",
)

from Component_1.db import Base, engine
import Component_1.models              # noqa: F401
import Component_4.tool_models         # noqa: F401
import Component_4.idempotency_models  # noqa: F401
import Component_6.approval_models     # noqa: F401
import Component_7.models   # noqa: F401

import api.credentials_models   # noqa: F401
import api.work_request_models  # noqa: F401

if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    print(f"Schema created/verified on: {engine.url}")