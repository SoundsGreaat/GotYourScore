"""Request correlation and rollback behavior at the application boundary."""

import json
import unittest
from unittest.mock import patch
from uuid import uuid4

from httpx import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.observability import JsonFormatter, request_id_var
from app.core.security import _correlate_oauth_exchange
from app.models import RefundCheck, RoleEnum
from tests.fixtures import PostgresTestCase


class OAuthCorrelationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_current_request_id_is_forwarded(self):
        exchange = Request("POST", "https://oauth2.googleapis.com/token")
        token = request_id_var.set("a" * 32)
        try:
            await _correlate_oauth_exchange(exchange)
        finally:
            request_id_var.reset(token)

        unrelated = Request("POST", "https://oauth2.googleapis.com/token")
        await _correlate_oauth_exchange(unrelated)
        self.assertEqual(exchange.headers["X-Request-ID"], "a" * 32)
        self.assertNotIn("X-Request-ID", unrelated.headers)


class RequestLogTests(PostgresTestCase):
    def test_committed_tracker_event_is_correlated_and_payload_free(self):
        actor = self.create_user(RoleEnum.QA)
        headers = self.session(actor)
        private_case = f"private-{uuid4().hex}"
        with self.assertLogs("app", "INFO") as captured:
            response = self.assert_status(self.client.post(
                "/api/refund-check",
                headers={**headers, "X-Request-ID": "untrusted-id"},
                params={"private": private_case},
                json={"related_case": private_case},
            ), 201)
        record_id = response.json()["id"]
        self.addCleanup(self.client.delete, f"/api/refund-check/{record_id}", headers=headers)
        correlation_id = response.headers["X-Request-ID"]
        self.assertRegex(correlation_id, r"^[0-9a-f]{32}$")
        formatted = [JsonFormatter().format(record) for record in captured.records]
        event, = [
            item for item in map(json.loads, formatted)
            if item["event"] == "tracker.record.created"
        ]
        self.assertEqual(event["request_id"], correlation_id)
        self.assertEqual(event["entity_id"], record_id)
        self.assertEqual(event["outcome"], "success")
        for secret in (private_case, "untrusted-id", headers["Cookie"]):
            self.assertNotIn(secret, " ".join(formatted))

    def test_failed_write_uses_safe_error_and_never_logs_a_commit(self):
        headers = self.session(self.create_user(RoleEnum.QA))
        private_case = f"rollback-{uuid4().hex}"
        private_error = "invalid provider key private@example.test"
        original_commit = AsyncSession.commit

        async def fail_write(session):
            if any(isinstance(record, RefundCheck) for record in session.new):
                await session.flush()
                raise RuntimeError(private_error)
            return await original_commit(session)

        with patch.object(AsyncSession, "commit", fail_write):
            with self.assertLogs("app", "INFO") as captured:
                response = self.assert_status(self.client.post(
                    "/api/refund-check",
                    headers=headers,
                    json={"related_case": private_case},
                ), 500)
        self.assertNotIn(private_error, response.text)
        formatted = [JsonFormatter().format(record) for record in captured.records]
        failure, = map(json.loads, formatted)
        self.assertEqual(failure["severity"], "ERROR")
        self.assertEqual(failure["request_id"], response.headers["X-Request-ID"])
        self.assertEqual(failure["error_type"], "RuntimeError")
        self.assertTrue(any("fail_write" in frame for frame in failure["traceback"]))
        for secret in (private_case, private_error, headers["Cookie"]):
            self.assertNotIn(secret, " ".join(formatted))
        records = self.assert_status(self.client.get("/api/refund-check", headers=headers), 200).json()
        self.assertNotIn(private_case, [record["related_case"] for record in records])

    def test_schema_rejection_is_correlated_without_the_submitted_values(self):
        headers = self.session(self.create_user(RoleEnum.QA))
        with self.assertLogs("app", "INFO") as captured:
            response = self.assert_status(self.client.post(
                "/api/ai/refactor", headers=headers, json={"html": ["private-value"]},
            ), 422)
        event, = [json.loads(JsonFormatter().format(record)) for record in captured.records]
        self.assertEqual(event["request_id"], response.headers["X-Request-ID"])
        self.assertEqual(event["reason_code"], "request_validation_error")
        self.assertNotIn("private-value", json.dumps(event))
