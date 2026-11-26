"""Regression coverage for structured logging redaction."""

import json
import logging
import unittest

from app.core.observability import JsonFormatter, log_event


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


if __name__ == "__main__":
    unittest.main()
