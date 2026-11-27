"""Regression coverage for structured logging redaction."""

import json
import logging
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.staticfiles import StaticFiles

from app.core.observability import JsonFormatter, log_event, request_id_var
from app.core.request_logging import RequestObservabilityMiddleware


class ObservabilityTests(unittest.TestCase):
    def test_formatter_emits_safe_structured_fields_not_message(self):
        logger = logging.getLogger("test.observability")
        records: list[logging.LogRecord] = []

        class Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        handler = Capture()
        logger.addHandler(handler)
        try:
            log_event(
                logger,
                "auth.login",
                "denied",
                level=logging.WARNING,
                reason_code="account_disabled",
                email="private@example.test",
                prompt="secret prompt",
            )
        finally:
            logger.removeHandler(handler)

        formatted = JsonFormatter().format(records[0])
        record = json.loads(formatted)
        self.assertEqual(record["event"], "auth.login")
        self.assertEqual(record["outcome"], "denied")
        self.assertEqual(record["reason_code"], "account_disabled")
        self.assertNotIn("private@example.test", formatted)
        self.assertNotIn("secret prompt", formatted)
        self.assertNotIn("message", record)

    def test_formatter_bounds_structured_values(self):
        logger = logging.getLogger("test.observability")
        with self.assertLogs(logger) as captured:
            log_event(
                logger, "batch.finished", "success", method="arbitrary",
                route="/some/path", status_code="private", elapsed_ms=float("nan"),
                reason_counts={"duplicate": 3, "bad value": 4, "wrong_type": True},
            )
        record = json.loads(JsonFormatter().format(captured.records[0]))
        self.assertEqual(record["method"], "OTHER")
        self.assertEqual(record["reason_counts"], {"duplicate": 3})
        for field in ("route", "status_code", "elapsed_ms"):
            self.assertNotIn(field, record)

    def test_request_outcome_survives_domain_warning_without_logging_raw_target(self):
        app = FastAPI()
        app.add_middleware(RequestObservabilityMiddleware)

        @app.get("/widgets/{item}")
        async def find_widget(item: str):
            log_event(logging.getLogger("app.test"), "widget.lookup", "denied", level=logging.WARNING)
            raise HTTPException(status_code=404)

        with TestClient(app) as client, self.assertLogs("app", "INFO") as captured:
            response = client.get("/widgets/private-path?private-query", headers={"X-Request-ID": "client-id"})
        self.assertEqual(response.status_code, 404)
        domain, summary = [json.loads(JsonFormatter().format(record)) for record in captured.records]
        self.assertEqual(domain["event"], "widget.lookup")
        self.assertEqual(summary["event"], "http.request.completed")
        self.assertEqual(summary["route"], "find_widget")
        self.assertEqual(summary["status_code"], 404)
        self.assertEqual(summary["outcome"], "expected_failure")
        self.assertGreaterEqual(summary["elapsed_ms"], 0)
        self.assertEqual(summary["request_id"], domain["request_id"])
        self.assertEqual(summary["request_id"], response.headers["X-Request-ID"])
        for value in ("private-path", "private-query", "client-id"):
            self.assertNotIn(value, json.dumps([domain, summary]))

    def test_successful_static_requests_are_quiet_but_failures_are_logged(self):
        app = FastAPI()
        app.add_middleware(RequestObservabilityMiddleware)
        app.mount("/assets", StaticFiles(directory=Path(__file__).parent), name="static")
        with TestClient(app) as client:
            with self.assertNoLogs("app", "INFO"):
                self.assertEqual(client.get("/assets/test_observability.py").status_code, 200)
            with self.assertLogs("app", "INFO") as captured:
                self.assertEqual(client.get("/assets/missing").status_code, 404)
        event, = captured.records
        self.assertEqual(event.route, "static")


class RequestDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_disconnect_is_not_a_success(self):
        async def streaming_app(scope, receive, send):
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send({"type": "http.response.body", "body": b"partial", "more_body": True})
            self.assertEqual((await receive())["type"], "http.disconnect")

        async def receive():
            return {"type": "http.disconnect"}

        async def send(message):
            pass

        scope = {"type": "http", "method": "GET", "route": SimpleNamespace(name="stream")}
        with self.assertLogs("app", "INFO") as captured:
            await RequestObservabilityMiddleware(streaming_app)(scope, receive, send)
        record, = captured.records
        self.assertEqual((record.event, record.outcome), ("http.request.incomplete", "disconnected"))
        self.assertEqual(record.status_code, 200)
        self.assertEqual(request_id_var.get(), "-")


if __name__ == "__main__":
    unittest.main()
