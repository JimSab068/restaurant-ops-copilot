"""
Unit tests for shared/queue.py. The boto3 SQS client is replaced with an
in-memory fake -- no real AWS or LocalStack needed.
"""

from __future__ import annotations

import json

import pytest

import shared.queue as queue_module


class FakeSqsClient:
    def __init__(self):
        self.sent = []
        self.deleted = []
        self._next_messages = []

    def send_message(self, **kwargs):
        self.sent.append(kwargs)
        return {"MessageId": "fake-id"}

    def receive_message(self, **kwargs):
        messages, self._next_messages = self._next_messages, []
        return {"Messages": messages}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs["ReceiptHandle"])


@pytest.fixture
def fake_client(monkeypatch):
    fake = FakeSqsClient()
    monkeypatch.setattr(queue_module, "_sqs_client", fake)
    yield fake
    monkeypatch.setattr(queue_module, "_sqs_client", None)


class TestPublish:

    def test_publish_sets_message_group_and_dedup_id(self, fake_client):
        queue_module.publish_work_request("tenant-1", "wr-1")

        assert len(fake_client.sent) == 1
        call = fake_client.sent[0]
        assert call["MessageGroupId"] == "tenant-1"
        assert call["MessageDeduplicationId"] == "wr-1"
        assert json.loads(call["MessageBody"]) == {
            "tenant_id": "tenant-1", "work_request_id": "wr-1",
        }

    def test_client_uses_configured_endpoint(self, monkeypatch):
        created = {}

        def create_client(service_name, **kwargs):
            created["service_name"] = service_name
            created["kwargs"] = kwargs
            return FakeSqsClient()

        monkeypatch.setattr(queue_module, "_sqs_client", None)
        monkeypatch.setattr(queue_module, "SQS_ENDPOINT_URL", "http://localstack:4566")
        monkeypatch.setattr(queue_module.boto3, "client", create_client)

        queue_module._client()

        assert created == {
            "service_name": "sqs",
            "kwargs": {
                "region_name": queue_module.AWS_REGION,
                "endpoint_url": "http://localstack:4566",
            },
        }


class TestPoll:

    def test_valid_message_is_parsed_into_queue_message(self, fake_client):
        fake_client._next_messages = [{
            "Body": json.dumps({"tenant_id": "t1", "work_request_id": "wr1"}),
            "ReceiptHandle": "rh1",
        }]

        messages = queue_module.poll_messages()

        assert len(messages) == 1
        assert messages[0].tenant_id == "t1"
        assert messages[0].work_request_id == "wr1"
        assert messages[0].receipt_handle == "rh1"

    def test_malformed_json_body_is_deleted_and_excluded(self, fake_client):
        fake_client._next_messages = [{"Body": "not valid json", "ReceiptHandle": "rh-bad"}]

        messages = queue_module.poll_messages()

        assert messages == []
        assert fake_client.deleted == ["rh-bad"]

    def test_missing_required_key_is_deleted_and_excluded(self, fake_client):
        fake_client._next_messages = [{
            "Body": json.dumps({"tenant_id": "t1"}),  # missing work_request_id
            "ReceiptHandle": "rh-missing",
        }]

        messages = queue_module.poll_messages()

        assert messages == []
        assert fake_client.deleted == ["rh-missing"]

    def test_valid_and_malformed_messages_in_same_batch(self, fake_client):
        fake_client._next_messages = [
            {"Body": json.dumps({"tenant_id": "t1", "work_request_id": "wr1"}), "ReceiptHandle": "rh-good"},
            {"Body": "garbage", "ReceiptHandle": "rh-bad"},
        ]

        messages = queue_module.poll_messages()

        assert len(messages) == 1
        assert messages[0].receipt_handle == "rh-good"
        assert fake_client.deleted == ["rh-bad"]


class TestDelete:

    def test_delete_message_calls_client(self, fake_client):
        queue_module.delete_message("rh-x")
        assert fake_client.deleted == ["rh-x"]