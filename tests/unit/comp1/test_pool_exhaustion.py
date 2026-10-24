import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine

import api.main as api_main_module
from Component_1 import db


def test_pool_exhaustion_times_out_and_marks_database_unhealthy(
    tmp_path, monkeypatch
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'pool-exhaustion.db'}",
        pool_size=1,
        max_overflow=0,
        pool_timeout=0.1,
    )
    monkeypatch.setattr(db, "engine", engine)

    try:
        with engine.connect():
            started = time.monotonic()
            assert db.health_check() is False
            elapsed = time.monotonic() - started
            assert 0.08 <= elapsed < 1.0
    finally:
        engine.dispose()


def test_readyz_returns_503_when_database_is_unhealthy(monkeypatch):
    monkeypatch.setattr(api_main_module, "health_check", lambda: False)

    response = TestClient(api_main_module.app).get("/readyz")

    assert response.status_code == 503
    assert response.json() == {"detail": "database is not reachable"}
