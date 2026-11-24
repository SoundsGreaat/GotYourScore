"""Tracker refactoring resolves independent versioned prompts."""

import json
from unittest.mock import AsyncMock, patch

from app.models import RoleEnum
from app.services import ai_service, system_prompt_service
from tests.fixtures import PostgresTestCase


class TrackerPromptTests(PostgresTestCase):
    def test_buffered_and_streamed_refactoring_select_the_tracker_prompt(self):
        headers = self.session(self.create_user(RoleEnum.QA))
        url = "/api/ai/refactor-comment"
        output = "<p>Refactored comment.</p>"
        seen = []

        async def refactor(html, db, *, prompt_key, fallback_prompt):
            seen.append((prompt_key, fallback_prompt))
            return output

        async def stream(html, db, *, prompt_key, fallback_prompt):
            seen.append((prompt_key, fallback_prompt))
            yield "final", output

        with (
            patch.object(ai_service, "get_openrouter_client"),
            patch.object(ai_service, "_refactor_html", new=AsyncMock(side_effect=refactor)),
            patch.object(ai_service, "_stream_refactor", stream),
        ):
            for tracker, key in (
                (None, "bf_comment_refactor"),
                ("bad-feedback", "bf_comment_refactor"),
                ("refund-check", "refund_check_comment_refactor"),
            ):
                with self.subTest(tracker=tracker):
                    json_body = {"html": "<p>Review.</p>"}
                    if tracker:
                        json_body["tracker"] = tracker
                    result = self.assert_status(self.client.post(url, headers=headers, json=json_body), 200)
                    self.assertEqual(result.json(), {"html": output})
                    result = self.assert_status(
                        self.client.post(f"{url}/stream", headers=headers, json=json_body), 200
                    )
                    self.assertEqual(json.loads(result.text), {"final": output})
                    expected = (key, ai_service.BF_COMMENT_SYSTEM_PROMPT)
                    self.assertEqual(seen[-2:], [expected, expected])
            self.assert_status(self.client.post(url, headers=headers, json={
                "html": "<p>Review.</p>", "tracker": "qa-score",
            }), 422)

        keys = {slot["key"] for slot in system_prompt_service.PROMPT_SLOTS}
        self.assertIn("bf_comment_refactor", keys)
        self.assertIn("refund_check_comment_refactor", keys)
