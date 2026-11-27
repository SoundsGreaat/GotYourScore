"""Notes from Score uses the ordinary deduction context in both modes."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from httpx import Request
from openai import APITimeoutError
from pydantic import ValidationError

from app.core.observability import JsonFormatter
from app.models import CaseTypeEnum, RoleEnum
from app.schemas.ai import NotesFromScoreIn
from app.services import ai_service, system_prompt_service
from tests.fixtures import PostgresTestCase


class NotesFromScoreTests(unittest.IsolatedAsyncioTestCase):
    async def _capture_buffered(
        self, custom_prompt: str | None
    ) -> list[dict[str, str]]:
        messages: list[dict[str, str]] = []
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(
                    create=AsyncMock(
                        side_effect=lambda **kwargs: (
                            messages.extend(kwargs["messages"])
                            or SimpleNamespace(
                                choices=[
                                    SimpleNamespace(
                                        message=SimpleNamespace(content="<p>Notes</p>")
                                    )
                                ]
                            )
                        )
                    )
                )
            )
        )
        with (
            patch.object(
                ai_service,
                "get_active_rules",
                new=AsyncMock(return_value={"items": [
                    {"error_name": "programs_features", "display_name": "Programs & Features"},
                ]}),
            ),
            patch.object(
                ai_service, "_active_system_prompt", new=AsyncMock(return_value=custom_prompt)
            ),
            patch.object(
                ai_service,
                "resolve_openrouter_request_config",
                new=AsyncMock(return_value=("test-model", {})),
            ),
            patch.object(ai_service, "get_openrouter_client", return_value=client),
        ):
            result = await ai_service.draft_notes_from_score(
                CaseTypeEnum.INITIAL_FIX,
                {"programs_features": 5},
                AsyncMock(),
            )
        self.assertEqual(result, "<p>Notes</p>")
        return messages

    async def _capture_streamed(
        self, custom_prompt: str | None
    ) -> tuple[str, str]:
        capture: dict[str, str] = {}

        async def fake_stream_completion(*, system_prompt, user_content, db_session):
            capture.update(system_prompt=system_prompt, user_content=user_content)
            yield "<p>Notes</p>"

        with (
            patch.object(
                ai_service,
                "get_active_rules",
                new=AsyncMock(return_value={"items": [
                    {"error_name": "programs_features", "display_name": "Programs & Features"},
                ]}),
            ),
            patch.object(
                ai_service, "_active_system_prompt", new=AsyncMock(return_value=custom_prompt)
            ),
            patch.object(ai_service, "_stream_completion", fake_stream_completion),
        ):
            events = [
                event
                async for event in ai_service.stream_draft_notes_from_score(
                    CaseTypeEnum.INITIAL_FIX,
                    {"programs_features": 5},
                    AsyncMock(),
                )
            ]
        self.assertEqual(events, [("d", "<p>Notes</p>"), ("final", "<p>Notes</p>")])
        return capture["system_prompt"], capture["user_content"]

    async def test_both_modes_include_ordinary_deductions_without_autofail(self):
        for custom_prompt in (None, "Custom reviewer voice and formatting."):
            with self.subTest(custom_prompt=custom_prompt):
                buffered = await self._capture_buffered(custom_prompt)
                streamed = await self._capture_streamed(custom_prompt)
                self.assertEqual(
                    (buffered[0]["content"], buffered[1]["content"]), streamed
                )
                expected_prompt = (
                    custom_prompt
                    if custom_prompt is not None
                    else ai_service.NOTES_FROM_SCORE_SYSTEM_PROMPT
                )
                self.assertEqual(streamed[0], expected_prompt)
                self.assertNotIn("Autofail", streamed[0])
                self.assertNotIn("Autofail", streamed[1])
                self.assertIn("Programs & Features", streamed[1])

    def test_request_schema_does_not_accept_autofail(self):
        base = {"case_type": "Initial Fix", "raw_scorecard": {}}
        for extra in ({"autofail": True}, {"autofail_reason": "Do not send this to AI."}):
            with self.assertRaises(ValidationError):
                NotesFromScoreIn(**base, **extra)
        with self.assertRaises(ValidationError):
            NotesFromScoreIn(**{**base, "raw_scorecard": {"autofail": 100}})

    def test_notes_prompt_slot_remains_single_existing_slot(self):
        slots = system_prompt_service.PROMPT_SLOTS
        self.assertEqual(
            [slot["key"] for slot in slots].count(
                ai_service.NOTES_FROM_SCORE_PROMPT_KEY
            ),
            1,
        )
        self.assertGreaterEqual(len(slots), 5)
        self.assertEqual(
            [slot["key"] for slot in slots].count(
                ai_service.AUTOFAIL_DETECTION_PROMPT_KEY
            ),
            1,
        )


class NotesFromScoreApiTests(PostgresTestCase):
    def test_ai_refactor_logs_empty_output_and_provider_timeout_without_content(self):
        actor = self.create_user(RoleEnum.QA)
        headers = self.session(actor)
        create = AsyncMock()
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch.object(ai_service, "get_openrouter_client", return_value=client):
            for reason in ("provider_output_empty", "provider_timeout"):
                with self.subTest(reason=reason):
                    create.return_value = SimpleNamespace(choices=[SimpleNamespace(
                        message=SimpleNamespace(content=""),
                    )])
                    create.side_effect = (
                        APITimeoutError(Request("POST", "https://provider.invalid/private-path"))
                        if reason == "provider_timeout" else None
                    )
                    with self.assertLogs("app", "INFO") as captured:
                        response = self.assert_status(self.client.post(
                            "/api/ai/refactor", headers=headers, json={"html": "<p>private note</p>"},
                        ), 502)
                    event, = [record for record in captured.records if record.event == "ai.refactor"]
                    self.assertEqual((event.user_id, event.reason_code), (actor.id, reason))
                    self.assertEqual(event.request_id, response.headers["X-Request-ID"])
                    if reason == "provider_timeout":
                        provider, = [
                            record for record in captured.records if record.event == "ai.provider.request"
                        ]
                        self.assertEqual(provider.reason_code, reason)
                        self.assertGreaterEqual(provider.elapsed_ms, 0)
                    serialized = " ".join(JsonFormatter().format(record) for record in captured.records)
                    for private in ("private note", "private-path", "provider.invalid"):
                        self.assertNotIn(private, serialized)

    def test_auto_scoring_cannot_persist_autofail_as_a_deduction(self):
        qa = self.session(self.create_user(RoleEnum.QA))
        agent = self.create_user(RoleEnum.SUPPORT)
        model_output = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content='{"autofail": 100, "Auto Fail": 100, "late_response": 5}',
        ))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(return_value=model_output),
        )))
        with (
            patch.object(ai_service, "get_openrouter_client", return_value=client),
            patch.object(ai_service, "get_active_rules", new=AsyncMock(return_value={"items": []})),
        ):
            response = self.assert_status(self.client.post("/api/reviews/auto-score", headers=qa, json={
                "agent_id": agent.id, "case_type": "Initial Fix", "transcript": "A sample support case.",
            }), 201).json()
        self.assertFalse(response["autofail"])
        self.assertEqual(response["final_score"], 95)
        self.assertEqual(set(response["scorecard_data"]["breakdown"]), {"late_response"})

    def test_ordinary_notes_contract_in_both_modes(self):
        headers = self.session(self.create_user(RoleEnum.QA))
        html = "<p>You didn't check Programs &amp; Features.</p>"
        payload = {
            "case_type": "Initial Fix", "raw_scorecard": {"programs_features": 5},
        }

        async def stream(case_type, scorecard, db, **kwargs):
            self.assertNotIn("autofail_reason", kwargs)
            self.assertEqual(scorecard, payload["raw_scorecard"])
            yield "d", html
            yield "final", html

        with (
            patch.object(ai_service, "get_openrouter_client"),
            patch.object(ai_service, "draft_notes_from_score", new=AsyncMock(return_value=html)) as draft,
            patch.object(ai_service, "stream_draft_notes_from_score", stream),
        ):
            base = "/api/ai/notes-from-score"
            buffered = self.assert_status(self.client.post(base, headers=headers, json=payload), 200)
            self.assertEqual(buffered.json(), {"notes_html": html})
            self.assertEqual(draft.await_args.args[1], payload["raw_scorecard"])
            self.assertNotIn("autofail_reason", draft.await_args.kwargs)

            streamed = self.assert_status(self.client.post(f"{base}/stream", headers=headers, json=payload), 200)
            self.assertEqual(
                [json.loads(line) for line in streamed.text.splitlines()],
                [{"d": html}, {"final": html}],
            )
            for url in (base, f"{base}/stream"):
                self.assert_status(self.client.post(url, headers=headers, json={
                    **payload, "autofail_reason": "Do not send this to AI.",
                }), 422)
                self.assert_status(self.client.post(url, headers=headers, json={
                    **payload, "case_type": "No Cases",
                }), 400)


if __name__ == "__main__":
    unittest.main()
