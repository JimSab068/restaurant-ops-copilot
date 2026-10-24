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
import Component_1.models              
import Component_4.tool_models      
import Component_4.idempotency_models  
import Component_6.approval_models     
import Component_7.models   

import api.credentials_models   
import api.work_request_models  

if __name__ == "__main__":
    Base.metadata.create_all(bind=engine)
    print(
        "Schema created/verified on: "
        f"{engine.url.render_as_string(hide_password=True)}"
    )