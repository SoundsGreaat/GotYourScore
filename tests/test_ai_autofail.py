"""Notes-to-Score Autofail detection contract (mocked provider)."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.models import CaseTypeEnum
from app.api.endpoints import admin
from app.api.endpoints import ai as ai_endpoint
from app.schemas.ai import ScoreIn
from app.services import ai_service, system_prompt_service


class AutofailDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_notes_score_uses_structured_contract_and_explicit_marker(self):
        notes = "<p>Agent disclosed account data (auto fail).</p>"
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=(
                            '{"scorecard":{"late_response":18,"invented":20},'
                            '"autofail_reason":"Account data was disclosed."}'
                        )
                    )
                )
            ]
        )
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(return_value=response))
            )
        )
        db = AsyncMock()
        with (
            patch.object(
                ai_service,
                "get_active_rules",
                new=AsyncMock(
                    return_value={
                        "items": [
                            {
                                "error_name": "late_response",
                                "display_name": "Late response",
                                "penalty_points": 5,
                                "category": "Communication",
                            }
                        ]
                    }
                ),
            ),
            patch.object(
                ai_service,
                "_active_system_prompt",
                new=AsyncMock(
                    side_effect=[
                        "Custom scoring prompt: one violation, one most specific rule.",
                        None,
                    ]
                ),
            ),
            patch.object(
                ai_service,
                "resolve_openrouter_request_config",
                new=AsyncMock(return_value=("mock-model", {})),
            ),
            patch.object(ai_service, "get_openrouter_client", return_value=client),
        ):
            scorecard, reason = await ai_service.analyze_support_ticket(
                notes, CaseTypeEnum.INITIAL_FIX, db, detect_autofail=True
            )

        self.assertEqual(scorecard, {"late_response": 5})
        self.assertEqual(reason, "Account data was disclosed.")
        prompt = client.chat.completions.create.await_args.kwargs["messages"][0][
            "content"
        ]
        self.assertIn("Custom scoring prompt", prompt)
        self.assertIn("late_response (Late response): deduct 5", prompt)
        self.assertIn('"scorecard"', prompt)
        self.assertIn("autofail_reason", prompt)
        self.assertIn("Autofail reason: ...", prompt)
        self.assertIn("visible notes explicitly", prompt)
        self.assertNotIn("flat JSON object", prompt)
        self.assertNotIn("_JSON_OUTPUT_CONTRACT", prompt)
        self.assertEqual(client.chat.completions.create.await_count, 1)

    async def test_legacy_flat_model_output_keeps_deductions_but_never_autofails(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content='{"late_response": 5, "autofail_reason": "A finding."}')
            )])),
        )))
        rules = {"items": [{
            "error_name": "late_response", "display_name": "Late response", "penalty_points": 5,
        }]}
        with (
            patch.object(ai_service, "get_active_rules", new=AsyncMock(return_value=rules)),
            patch.object(ai_service, "_active_system_prompt", new=AsyncMock(return_value="Return a flat JSON object.")),
            patch.object(
                ai_service, "resolve_openrouter_request_config", new=AsyncMock(return_value=("mock-model", {}))
            ),
            patch.object(ai_service, "get_openrouter_client", return_value=client),
        ):
            result = await ai_service.analyze_support_ticket(
                "<p>Replied late (Autofail).</p>", CaseTypeEnum.INITIAL_FIX, AsyncMock(), detect_autofail=True,
            )
        self.assertEqual(result, ({"late_response": 5}, None))

    def test_marker_requires_visible_explicit_text(self):
        self.assertTrue(
            ai_service._has_explicit_autofail_marker(
                "<p>Account data disclosed (auto fail)</p>"
            )
        )
        self.assertTrue(
            ai_service._has_explicit_autofail_marker(
                "<p>You received auto fail for this case because data was disclosed.</p>"
            )
        )
        self.assertTrue(
            ai_service._has_explicit_autofail_marker("<p>Autofail: disclosure.</p>")
        )
        self.assertTrue(
            ai_service._has_explicit_autofail_marker("<p>Autofail reason: disclosure.</p>")
        )
        self.assertTrue(
            ai_service._has_explicit_autofail_marker(
                "<p><strong>Auto</strong>fail for this case.</p>"
            )
        )
        # This is only the lexical gate; the detection model must reject
        # negated/hypothetical mentions when choosing the structured reason.
        self.assertTrue(
            ai_service._has_explicit_autofail_marker(
                "<p>This is not an Autofail.</p>"
            )
        )
        self.assertFalse(
            ai_service._has_explicit_autofail_marker(
                '<p title="Autofail: disclosure">Routine review</p>'
            )
        )
        self.assertFalse(
            ai_service._has_explicit_autofail_marker(
                "<script>Autofail: disclosure</script><p>Routine review</p>"
            )
        )

    def test_detected_reason_is_validated(self):
        self.assertEqual(ai_service._sanitize_autofail_reason(" reason "), "reason")
        for invalid in (None, "", "  ", "x" * 2001, "bad\x00reason", 42):
            with self.subTest(invalid=repr(invalid)):
                self.assertIsNone(ai_service._sanitize_autofail_reason(invalid))

    def test_prompt_default_is_registered_only_for_autofail_slot(self):
        slots = system_prompt_service.PROMPT_SLOTS
        autofail_slot = next(
            slot for slot in slots if slot["key"] == "autofail_detection"
        )
        self.assertEqual(
            autofail_slot["default_content"],
            system_prompt_service.AUTOFAIL_DETECTION_SYSTEM_PROMPT,
        )
        self.assertFalse(
            any(
                "default_content" in slot
                for slot in slots
                if slot["key"] != "autofail_detection"
            )
        )
        preview_prompt = ai_service._build_scoring_prompt(
            [], autofail_detection_prompt="Detection rules."
        )
        self.assertIn('"scorecard"', preview_prompt)
        self.assertNotIn("single flat JSON object", preview_prompt)
        legacy_prompt = ai_service._build_scoring_prompt([])
        self.assertIn("single flat JSON object", legacy_prompt)

    async def test_admin_prompt_context_forwards_only_registered_default(self):
        with patch.object(
            system_prompt_service,
            "grouped_versions",
            new=AsyncMock(return_value=[]),
        ):
            context = await admin._prompts_context(AsyncMock())
        autofail_slot = next(
            slot for slot in context["slots"] if slot["key"] == "autofail_detection"
        )
        self.assertEqual(
            autofail_slot["default_content"],
            system_prompt_service.AUTOFAIL_DETECTION_SYSTEM_PROMPT,
        )
        self.assertFalse(
            any(
                "default_content" in slot
                for slot in context["slots"]
                if slot["key"] != "autofail_detection"
            )
        )

    async def test_score_api_computes_zero_without_moving_reason_into_scorecard(self):
        lease = SimpleNamespace(release=AsyncMock())
        with (
            patch.object(
                ai_endpoint,
                "reserve_ai_request",
                new=AsyncMock(return_value=lease),
            ),
            patch.object(
                ai_service,
                "analyze_support_ticket",
                new=AsyncMock(
                    return_value=(
                        {"late_response": 5},
                        "Explicitly documented critical failure.",
                    )
                ),
            ) as analyze,
        ):
            result = await ai_endpoint.score_notes(
                ScoreIn(
                    html="<p>notes</p>",
                    case_type=CaseTypeEnum.INITIAL_FIX,
                ),
                AsyncMock(),
                SimpleNamespace(id=1),
            )
        self.assertEqual(result.scorecard, {"late_response": 5})
        self.assertEqual(result.total_deduction, 5)
        self.assertEqual(result.final_score, 0)
        self.assertEqual(
            result.autofail_reason,
            "Explicitly documented critical failure.",
        )
        self.assertTrue(analyze.await_args.kwargs["detect_autofail"])
        lease.release.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
