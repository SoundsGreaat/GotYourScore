"""Notes-to-Score Autofail detection contract (mocked provider)."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.api.endpoints import admin
from app.api.endpoints import ai as ai_endpoint
from app.models import CaseTypeEnum
from app.schemas.ai import ScoreIn
from app.services import ai_service, system_prompt_service


class AutofailDetectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_notes_score_uses_structured_contract_and_explicit_marker(self):
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
                    side_effect=lambda key, db: (
                        "Stored detection prompt."
                        if key == ai_service.AUTOFAIL_DETECTION_PROMPT_KEY
                        else "Custom scoring prompt: one violation, one most specific rule."
                    ),
                ),
            ),
            patch.object(
                ai_service,
                "resolve_openrouter_request_config",
                new=AsyncMock(return_value=("mock-model", {})),
            ),
            patch.object(ai_service, "get_openrouter_client", return_value=client),
        ):
            labels = ("af", "AF", "Autofail", "AUTO FAIL", "Auto-Fail", "auto‑fail")
            for label in labels:
                with self.subTest(label=label):
                    scorecard, reason = await ai_service.analyze_support_ticket(
                        f"<p>Agent disclosed account data - {label}.</p>",
                        CaseTypeEnum.INITIAL_FIX,
                        db,
                        detect_autofail=True,
                    )
                    self.assertEqual(scorecard, {"late_response": 5})
                    self.assertEqual(reason, "Account data was disclosed.")
            prompt = client.chat.completions.create.await_args.kwargs["messages"][0]["content"]
            ordinary_scorecard, no_reason = await ai_service.analyze_support_ticket(
                "<p>Agent disclosed account data; reference AF-123.</p>",
                CaseTypeEnum.INITIAL_FIX,
                db,
                detect_autofail=True,
            )

        self.assertEqual(ordinary_scorecard, {"late_response": 5})
        self.assertIsNone(no_reason)
        self.assertIn("Custom scoring prompt", prompt)
        self.assertIn("late_response (Late response): deduct 5", prompt)
        self.assertIn('"scorecard"', prompt)
        self.assertIn("autofail_reason", prompt)
        self.assertIn("Autofail reason: ...", prompt)
        self.assertIn("Stored detection prompt.", prompt)
        self.assertIn("Skipped the check (AF)", " ".join(prompt.split()))
        self.assertNotIn("flat JSON object", prompt)
        self.assertNotIn("_JSON_OUTPUT_CONTRACT", prompt)
        self.assertEqual(client.chat.completions.create.await_count, len(labels) + 1)

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
        for label in (
            "autofail", "AUTOFAIL", "Auto Fail", "auto fail", "auto-fail",
            "AUTO-FAIL", "AuTo — fAiL", "auto‑fail", "<strong>Auto</strong>fail",
        ):
            with self.subTest(label=label):
                self.assertTrue(ai_service._has_explicit_autofail_marker(f"<p>Skipped the check - {label}.</p>"))

        for notes in (
            "You didn't check LMI Calling card - af",
            "Skipped the check — AF", "Skipped the check–Af", "Skipped the check (aF)",
            "Skipped the check [AF]", "Skipped the check （AF）",
            "AF: skipped the check", 'AF reason: "Skipped the check."',
            "AF - skipped the check", "AF- skipped the check", "AF due to a skipped check",
            "You received AF for skipping the check", "<p><b>AF</b></p><p>Skipped the check.</p>",
            "Skipped the check (<strong>A</strong>F)", "Autofail reason: disclosure.",
            # The model, not the lexical gate, handles negation.
            "This is not an Autofail.",
        ):
            with self.subTest(notes=notes):
                self.assertTrue(ai_service._has_explicit_autofail_marker(notes))

        for notes in (
            "", "AF", "(af)", "AF:", "AF-123", "Reference AF-123 for the check.",
            "This was logged as team-af: reviewed.", "The AF app was checked.",
            "Safe and sound.", "The auto-failure guide was consulted.",
            '<p title="Autofail: disclosure">Routine review</p>',
            '<p title="AF: disclosure">Routine review</p>',
            "<script>Autofail: disclosure</script><p>Routine review</p>",
            "<!-- AF: disclosure --><p>Routine review</p>",
            "<p>auto</p><p>fail</p>", "tri<b>AF</b>ge: routine",
        ):
            with self.subTest(notes=notes):
                self.assertFalse(ai_service._has_explicit_autofail_marker(notes))

    def test_detected_reason_is_validated(self):
        self.assertEqual(ai_service._sanitize_autofail_reason(" reason "), "reason")
        for invalid in (None, "", "  ", "x" * 2001, "bad\x00reason", 42):
            with self.subTest(invalid=repr(invalid)):
                self.assertIsNone(ai_service._sanitize_autofail_reason(invalid))

    def test_autofail_default_is_only_registered_for_its_own_slot(self):
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
                slot.get("default_content")
                == system_prompt_service.AUTOFAIL_DETECTION_SYSTEM_PROMPT
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

    async def test_admin_prompt_context_forwards_defaults_by_registered_slot(self):
        with patch.object(
            system_prompt_service,
            "grouped_versions",
            new=AsyncMock(return_value=[]),
        ):
            context = await admin._prompts_context(AsyncMock())
        slots = [slot for category in context["categories"] for slot in category["slots"]]
        autofail_slot = next(slot for slot in slots if slot["key"] == "autofail_detection")
        self.assertEqual(
            autofail_slot["default_content"],
            system_prompt_service.AUTOFAIL_DETECTION_SYSTEM_PROMPT,
        )
        self.assertEqual(
            {slot["key"]: slot["default_content"] for slot in slots if "default_content" in slot},
            {
                slot["key"]: slot["default_content"]
                for slot in system_prompt_service.PROMPT_SLOTS
                if "default_content" in slot
            },
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
