from __future__ import annotations

import json

import pytest

import api.rotate_tenant_api_keys as rotation


def test_publish_fixtures_secret_stores_json_without_printing_keys(monkeypatch, capsys):
    calls = []

    class FakeSecretsManager:
        def put_secret_value(self, **kwargs):
            calls.append(kwargs)

    monkeypatch.setattr(
        rotation.boto3,
        "client",
        lambda service, region_name: FakeSecretsManager(),
    )
    fixtures = {"tenant-1": {"api_key": "test-secret-key"}}

    rotation._publish_fixtures_secret("fixture-secret-arn", fixtures)

    assert len(calls) == 1
    assert calls[0]["SecretId"] == "fixture-secret-arn"
    assert json.loads(calls[0]["SecretString"]) == fixtures
    assert capsys.readouterr().out == ""


def test_secret_failure_explains_that_keys_were_already_rotated(monkeypatch):
    from botocore.exceptions import EndpointConnectionError

    class FailedSecretsManager:
        def put_secret_value(self, **kwargs):
            raise EndpointConnectionError(endpoint_url="https://secrets.example")

    monkeypatch.setattr(
        rotation.boto3,
        "client",
        lambda service, region_name: FailedSecretsManager(),
    )

    with pytest.raises(RuntimeError, match="keys were rotated.*could not be updated"):
        rotation._publish_fixtures_secret(
            "fixture-secret-arn",
            {"tenant-1": {"api_key": "test-secret-key"}},
        )


def test_rotation_requires_exactly_one_fixture_destination():
    with pytest.raises(ValueError, match="exactly one"):
        rotation.rotate_keys()


def test_secret_destination_is_published_after_database_commit(monkeypatch):
    events = []

    class FakeSession:
        def commit(self):
            events.append("commit")

        def rollback(self):
            events.append("rollback")

        def close(self):
            events.append("close")

    fixtures = {"tenant-1": {"api_key": "new-key"}}
    monkeypatch.setattr(rotation, "get_session", FakeSession)
    monkeypatch.setattr(rotation, "_tenant_fixtures", lambda session: fixtures)
    monkeypatch.setattr(
        rotation,
        "_publish_fixtures_secret",
        lambda secret_arn, value: events.append(("publish", secret_arn, value)),
    )

    count = rotation.rotate_keys(secret_arn="fixture-secret-arn")

    assert count == 1
    assert events == [
        "commit",
        ("publish", "fixture-secret-arn", fixtures),
        "close",
    ]
