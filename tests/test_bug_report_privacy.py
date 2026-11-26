"""Privacy-safe preparation and private OpenRouter screening contract."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from app.schemas.bug_reports import BugReportInput
from app.services import ai_service, bug_report_service


def _response(content: str, finish_reason: str = "stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason=finish_reason,
        message=SimpleNamespace(content=content),
    )])


class BugReportPrivacyTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_trims_limits_and_forbids_extra_fields(self):
        report = BugReportInput(
            title="  title ", steps=" steps ", actual=" actual ",
            expected=" expected ",
        )
        self.assertEqual(report.title, "title")
        with self.assertRaises(ValidationError):
            BugReportInput(
                title="title", steps="steps", actual="actual",
                expected="expected", extra="no",
            )

    async def test_provider_uses_private_route_and_admin_model(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(return_value=_response(
                '{"redactions":[{"field":"actual","text":"Private Name"}],'
                '"unclear_fields":[]}'
            ))
        )))
        db = AsyncMock()
        with (
            patch.object(ai_service, "get_settings", return_value=SimpleNamespace(
                OPENROUTER_API_KEY="test-key"
            )),
            patch.object(ai_service.app_setting_service, "get_value",
                         new=AsyncMock(return_value={"model": "admin/model", "reasoning_effort": "high"})),
            patch.object(ai_service, "_active_system_prompt",
                         new=AsyncMock(return_value="Supplemental prompt")),
            patch.object(ai_service, "get_openrouter_client", return_value=client),
        ):
            result = await ai_service.screen_bug_report_privacy(
                {"actual": "Private Name has an error."}, db
            )
        self.assertEqual(result["redactions"][0]["text"], "Private Name")
        kwargs = client.chat.completions.create.await_args.kwargs
        self.assertEqual(kwargs["model"], "admin/model")
        messages = kwargs["messages"]
        self.assertEqual(messages[0]["content"], "Supplemental prompt")
        mandatory = messages[1]["content"].lower()
        for category in (
            "case and ticket ids",
            "confidential customer or case notes",
            "internal procedures",
            "even when no personal identifier is present",
        ):
            self.assertIn(category, mandatory)
        self.assertEqual(kwargs["extra_body"], {"provider": {
            "data_collection": "deny", "require_parameters": True, "zdr": True
        }})
        self.assertNotIn("reasoning", kwargs["extra_body"])

    async def test_incomplete_or_invalid_provider_output_fails_closed(self):
        for response in (
            _response('{"redactions":[],"unclear_fields":[]}', "length"),
            _response('{"redactions":[{"field":"actual","text":"invented"}],'
                      '"unclear_fields":[]}'),
        ):
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
                create=AsyncMock(return_value=response)
            )))
            with (
                patch.object(ai_service, "get_settings", return_value=SimpleNamespace(
                    OPENROUTER_API_KEY="test-key"
                )),
                patch.object(ai_service.app_setting_service, "get_value",
                             new=AsyncMock(return_value={})),
                patch.object(ai_service, "_active_system_prompt",
                             new=AsyncMock(return_value=None)),
                patch.object(ai_service, "get_openrouter_client", return_value=client),
                self.assertRaises(ai_service.BugReportPrivacyError),
            ):
                await ai_service.screen_bug_report_privacy(
                    {"actual": "Known actual text."}, AsyncMock()
                )

    async def test_deterministic_redaction_and_server_formatted_report(self):
        report = BugReportInput(
            title="Crash at https://example.test/path",
            steps="Open it and contact alice@example.com",
            actual="Bearer abcdefghijklmnopqrstuvwxyz failed <script>alert(1)</script>",
            expected="The page should load.",
            environment="Browser @support",
            context="",
        )
        received = {}

        async def screening(fields, _db):
            received.update(fields)
            return {"redactions": [], "unclear_fields": []}

        with patch.object(bug_report_service, "screen_bug_report_privacy", screening):
            prepared = await bug_report_service.prepare_report(AsyncMock(), report)
        self.assertNotIn("example.test", str(received))
        self.assertNotIn("alice@example.com", str(received))
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", str(received))
        self.assertIn("## Problem", prepared.body)
        self.assertIn("## Problem\n\n\\[REDACTED\\] failed", prepared.body)
        self.assertIn("## Environment\n\nBrowser @\u200bsupport", prepared.body)
        self.assertNotIn("## Actual behavior", prepared.body)
        self.assertIn("Submitted via the GotYourScore website.", prepared.body)
        self.assertNotIn("<script>", prepared.body)
        self.assertNotIn("@support", prepared.body)
        self.assertEqual(prepared.title, "Crash at [REDACTED]")

    def test_overlapping_literal_spans_are_redacted_as_their_union(self):
        fields = {"actual": "ABCDEFGHI--DEFGHI"}
        bug_report_service._apply_spans(fields, [
            {"field": "actual", "text": "ABCDEF"},
            {"field": "actual", "text": "DEFGHI"},
        ])
        self.assertEqual(fields["actual"], "[REDACTED]--[REDACTED]")

    def test_unicode_substance_plain_title_and_common_secret_patterns(self):
        self.assertTrue(bug_report_service._has_substance("[REDACTED] Ошибка"))
        self.assertEqual(
            bug_report_service._deterministic_redact("gotyourscore.save"),
            "gotyourscore.save",
        )
        self.assertEqual(
            bug_report_service._format_plain_title("  Добрый\tдень\u202e "),
            "Добрый день",
        )
        for field, secret in (
            ("ghp_abcdefghijklmnopqrstuvwxyz012345", "abcdefghijklmnopqrstuvwxyz012345"),
            ("gho_abcdefghijklmnopqrstuvwxyz012345", "abcdefghijklmnopqrstuvwxyz012345"),
            ("github_pat_abcdefghijklmnopqrstuvwxyz012345", "abcdefghijklmnopqrstuvwxyz012345"),
            ("Authorization: Bearer foobar", "foobar"),
            ("password: example-secret", "example-secret"),
            ("-----BEGIN PRIVATE KEY-----\nfixture-key-payload\n-----END PRIVATE KEY-----", "fixture-key-payload"),
            ("email hidden\u200b@example.com", "example.com"),
        ):
            with self.subTest(field=field):
                self.assertNotIn(secret, bug_report_service._deterministic_redact(field))

    async def test_redaction_that_removes_required_substance_is_rejected(self):
        report = BugReportInput(
            title="An issue", steps="Do the thing", actual="A named person",
            expected="It should work",
        )
        async def screening(_fields, _db):
            return {
                "redactions": [{"field": "actual", "text": "A named person"}],
                "unclear_fields": [],
            }
        with (
            patch.object(bug_report_service, "screen_bug_report_privacy", screening),
            self.assertRaises(bug_report_service.UnsuitableBugReportError) as caught,
        ):
            await bug_report_service.prepare_report(AsyncMock(), report)
        self.assertEqual(caught.exception.fields, ["actual"])

    def test_request_rejects_overlong_values(self):
        with self.assertRaises(ValidationError):
            BugReportInput(
                title="t" * 121, steps="s", actual="a", expected="e"
            )
