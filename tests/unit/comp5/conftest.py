"""
test_rls_isolation.py is a pure integration test: it assumes the schema
and RLS policies already exist (created by `python -m Component_5.enable_rls`
against RLS_ADMIN_DATABASE_URL), and only seeds/cleans rows through
admin_engine/app_engine.

The top-level `clean_db` fixture in tests/conftest.py is autouse, so it
would otherwise run for every test in this directory too. It connects as
the postgres superuser and calls Base.metadata.drop_all()/create_all(),
which knows nothing about RLS policies -- it would silently strip RLS
from the schema this whole suite depends on, and do it again between
tests, corrupting seeded_tenants' session-scoped fixture data.

Overriding clean_db here as a no-op keeps the RLS-configured schema
(and the tenant_isolation policies on it) untouched for the whole
session, which is exactly what this suite requires.
"""
import pytest


@pytest.fixture(autouse=True)
def clean_db():
    """No-op override: this suite must not drop/recreate the schema."""
    yield