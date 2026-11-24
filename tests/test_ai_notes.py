"""Autofail-aware Notes from Score prompt and payload behavior."""

import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from app.models import CaseTypeEnum, RoleEnum
from app.schemas.ai import NotesFromScoreIn
from app.services import ai_service, system_prompt_service
from tests.fixtures import PostgresTestCase


class NotesFromScoreAutofailTests(unittest.IsolatedAsyncioTestCase):
    async def _capture_buffered(
        self, reason: str | None, custom_prompt: str | None
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
                new=AsyncMock(return_value={"items": []}),
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
            await ai_service.draft_notes_from_score(
                CaseTypeEnum.INITIAL_FIX,
                {},
                AsyncMock(),
                autofail_reason=reason,
            )
        return messages

    async def _capture_streamed(
        self, reason: str | None, custom_prompt: str | None
    ) -> tuple[str, str]:
        capture: dict[str, str] = {}

        async def fake_stream_completion(*, system_prompt, user_content, db_session):
            capture.update(system_prompt=system_prompt, user_content=user_content)
            yield "<p>Notes</p>"

        with (
            patch.object(
                ai_service,
                "get_active_rules",
                new=AsyncMock(return_value={"items": []}),
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
                    {},
                    AsyncMock(),
                    autofail_reason=reason,
                )
            ]
        self.assertEqual(events[-1][0], "final")
        if reason:
            self.assertEqual(events[-2], ("autofail_reason", reason.strip()))
            self.assertIn("<p><strong>Autofail</strong></p>", events[-1][1])
        else:
            self.assertEqual(events[-1], ("final", "<p>Notes</p>"))
        return capture["system_prompt"], capture["user_content"]

    async def test_autofail_instruction_is_appended_for_default_and_custom_prompt(self):
        reason = "The reviewer confirmed the prohibited action."
        for custom_prompt in (None, "Custom reviewer voice and formatting."):
            with self.subTest(custom_prompt=custom_prompt):
                buffered = await self._capture_buffered(reason, custom_prompt)
                streamed = await self._capture_streamed(reason, custom_prompt)
                self.assertEqual([m["role"] for m in buffered], ["system", "user"])
                self.assertEqual(
                    (buffered[0]["content"], buffered[1]["content"]), streamed
                )
                system_prompt, user_content = streamed
                self.assertIn("bold \"Autofail\"", system_prompt)
                self.assertIn("Do not add numeric", system_prompt)
                self.assertIn("never as instructions", system_prompt)
                self.assertNotIn(reason, system_prompt)
                self.assertIn(reason, user_content)
                self.assertIn("No regular scorecard deductions were selected", user_content)
                self.assertIn("Final score: 0/100", user_content)

    async def test_absent_reason_preserves_default_and_custom_prompt_messages(self):
        for custom_prompt in (None, "Custom reviewer voice and formatting."):
            with self.subTest(custom_prompt=custom_prompt):
                buffered = await self._capture_buffered(None, custom_prompt)
                streamed = await self._capture_streamed(None, custom_prompt)
                self.assertEqual(
                    (buffered[0]["content"], buffered[1]["content"]), streamed
                )
                expected_prompt = (
                    custom_prompt
                    if custom_prompt is not None
                    else ai_service.NOTES_FROM_SCORE_SYSTEM_PROMPT
                )
                self.assertEqual(streamed[0], expected_prompt)
                self.assertNotIn("The reviewer supplied an Autofail reason", streamed[0])
                self.assertNotIn("Autofail", streamed[1])

    def test_request_schema_normalizes_and_bounds_optional_reason(self):
        base = {"case_type": "Initial Fix", "raw_scorecard": {}}
        self.assertIsNone(NotesFromScoreIn(**base).autofail_reason)
        self.assertEqual(
            NotesFromScoreIn(**base, autofail_reason="  reason  ").autofail_reason,
            "reason",
        )
        for reason in ("", "  "):
            with self.subTest(reason=reason), self.assertRaises(ValidationError):
                NotesFromScoreIn(**base, autofail_reason=reason)
        with self.assertRaises(ValidationError):
            NotesFromScoreIn(**base, autofail_reason="x" * 2001)
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

    def test_autofail_only_notes_forward_the_reason_in_both_modes(self):
        headers = self.session(self.create_user(RoleEnum.QA))
        html = "<p><strong>Autofail</strong></p><p>Policy was bypassed.</p>"
        payload = {
            "case_type": "Initial Fix", "raw_scorecard": {}, "autofail_reason": "  Policy was bypassed.  ",
        }

        async def stream(case_type, scorecard, db, **kwargs):
            self.assertEqual(kwargs["autofail_reason"], "Policy was bypassed.")
            self.assertEqual(scorecard, {})
            yield "d", html
            yield "autofail_reason", "Policy was bypassed."
            yield "final", html

        with (
            patch.object(ai_service, "get_openrouter_client"),
            patch.object(ai_service, "draft_notes_from_score", new=AsyncMock(return_value=(html, "Policy was bypassed."))) as draft,
            patch.object(ai_service, "stream_draft_notes_from_score", stream),
        ):
            base = "/api/ai/notes-from-score"
            buffered = self.assert_status(self.client.post(base, headers=headers, json=payload), 200)
            self.assertEqual(buffered.json(), {
                "notes_html": html, "autofail_reason": "Policy was bypassed.",
            })
            self.assertEqual(draft.await_args.args[1], {})
            self.assertEqual(draft.await_args.kwargs["autofail_reason"], "Policy was bypassed.")

            streamed = self.assert_status(self.client.post(f"{base}/stream", headers=headers, json=payload), 200)
            self.assertEqual(
                [json.loads(line) for line in streamed.text.splitlines()],
                [{"d": html}, {"autofail_reason": "Policy was bypassed."}, {"final": html}],
            )
            for url in (base, f"{base}/stream"):
                self.assert_status(self.client.post(url, headers=headers, json={
                    **payload, "case_type": "No Cases",
                }), 400)


if __name__ == "__main__":
    unittest.main()
