"""Review lifecycle, duplicate prevention, and score persistence."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Barrier
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete

from app.db.database import async_session_factory
from app.models import CaseTypeEnum, Review, RoleEnum
from app.models.scorecard import ScorecardItem, ScorecardTemplate
from app.services.reporting_period import reporting_period_for
from tests.fixtures import PostgresTestCase


class ReviewTests(PostgresTestCase):
    def test_autofail_preview_save_and_rescore_keep_regular_deductions(self):
        agent = self.create_user(RoleEnum.SUPPORT)
        headers = self.session(self.create_user(RoleEnum.QA))
        path = "/api/reviews"
        base = {"support_agent_id": agent.id, "case_type": "Initial Fix"}
        deduction = {"late_response": 5}

        def preview(raw, autofail=False):
            return self.assert_status(self.client.post(f"{path}/score-preview", headers=headers, json={
                **base, "raw_scorecard": raw, "autofail": autofail,
            }), 200).json()

        def save(raw, autofail=False):
            options = {"autofail": True, "autofail_reason": "  Critical handling mistake. \n"} if autofail else {}
            return self.assert_status(self.client.post(path, headers=headers, json={
                **base, "raw_scorecard": raw, **options,
            }), 201).json()

        normal = save(deduction)
        self.assertEqual(normal["final_score"], 95)
        self.assertFalse(normal["autofail"])
        self.assertIsNone(normal["autofail_reason"])

        self.assertEqual(preview({}, autofail=True), {
            "breakdown": {}, "total_penalty": 0, "final_score": 0,
        })
        autofail_only = save({}, autofail=True)
        self.assertEqual(autofail_only["final_score"], 0)
        self.assertEqual(autofail_only["scorecard_data"]["final_score"], 0)
        self.assertEqual(autofail_only["scorecard_data"]["breakdown"], {})

        expected = preview(deduction, autofail=True)
        self.assertEqual(expected["final_score"], 0)
        self.assertEqual(expected["total_penalty"], 10)
        self.assertEqual(expected["breakdown"], {
            "late_response": {"deducted": 5, "multiplier": 2, "final_penalty": 10},
        })
        combined = save(deduction, autofail=True)
        combined_path = f"{path}/{combined['id']}"
        reloaded = self.assert_status(self.client.get(combined_path, headers=headers), 200).json()
        self.assertTrue(reloaded["autofail"])
        self.assertEqual(reloaded["autofail_reason"], "Critical handling mistake.")
        self.assertEqual(reloaded["final_score"], 0)
        self.assertEqual(reloaded["scorecard_data"]["breakdown"], expected["breakdown"])
        self.assertNotIn("autofail_reason", reloaded["scorecard_data"])
        # The actual error counts in later cases, but Autofail does not
        # add an occurrence of any error, even when it was the only finding.
        self.assertEqual(preview(deduction)["breakdown"]["late_response"]["multiplier"], 3)

        rescored = self.assert_status(self.client.patch(combined_path, headers=headers, json={
            "raw_scorecard": deduction, "no_multiplier_keys": ["late_response"],
        }), 200).json()
        self.assertTrue(rescored["autofail"])
        self.assertEqual(rescored["final_score"], 0)
        self.assertEqual(rescored["scorecard_data"]["total_penalty"], 5)
        restored = self.assert_status(self.client.patch(combined_path, headers=headers, json={
            "autofail": False, "autofail_reason": "Discard this reason.",
        }), 200).json()
        self.assertFalse(restored["autofail"])
        self.assertIsNone(restored["autofail_reason"])
        self.assertEqual(restored["final_score"], 95)
        self.assertEqual(restored["scorecard_data"], {**rescored["scorecard_data"], "final_score": 95})
        enabled = self.assert_status(self.client.patch(combined_path, headers=headers, json={
            "autofail": True, "autofail_reason": "Updated reason.",
        }), 200).json()
        self.assertEqual(enabled["autofail_reason"], "Updated reason.")
        self.assertEqual(enabled["scorecard_data"], rescored["scorecard_data"])
        self.assertEqual(enabled["final_score"], 0)

    def test_autofail_reason_validation_and_pending_completion(self):
        agent = self.create_user(RoleEnum.SUPPORT)
        headers = self.session(self.create_user(RoleEnum.ADMIN))
        path = "/api/reviews"
        base = {"support_agent_id": agent.id, "case_type": "Initial Fix"}
        for save_for_later in (False, True):
            for reason_fields in ({}, {"autofail_reason": " \t\n\u00a0 "}):
                with self.subTest(save_for_later=save_for_later, reason=reason_fields):
                    self.assert_status(self.client.post(path, headers=headers, json={
                        **base, "autofail": True, "save_for_later": save_for_later, **reason_fields,
                    }), 422)

        draft = self.assert_status(self.client.post(path, headers=headers, json={
            **base, "save_for_later": True, "autofail": True, "autofail_reason": "Draft reason",
        }), 201).json()
        draft_path = f"{path}/{draft['id']}"
        self.assertTrue(draft["autofail"])
        self.assertIsNone(draft["final_score"])
        self.assertEqual(draft["status"], "pending")
        self.assertEqual(
            self.client.get(draft_path, headers=headers).json()["autofail_reason"], "Draft reason"
        )
        for invalid in (None, "  "):
            self.assert_status(self.client.patch(draft_path, headers=headers, json={
                "raw_scorecard": {}, "autofail_reason": invalid,
            }), 422)
        completed = self.assert_status(self.client.patch(
            draft_path, headers=headers, json={"raw_scorecard": {}},
        ), 200).json()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["final_score"], 0)
        self.assertEqual(completed["autofail_reason"], "Draft reason")

        delegated = self.assert_status(self.client.post(f"{path}/pending", headers=headers, json=base), 201).json()
        delegated_path = f"{path}/{delegated['id']}"
        self.assert_status(self.client.patch(delegated_path, headers=headers, json={
            "autofail": True, "raw_scorecard": {},
        }), 422)
        completed = self.assert_status(self.client.patch(delegated_path, headers=headers, json={
            "autofail": True, "autofail_reason": "Delegated reason", "raw_scorecard": {},
        }), 200).json()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["final_score"], 0)
        self.assertEqual(completed["autofail_reason"], "Delegated reason")

        no_cases = {**base, "case_type": "No Cases"}
        self.assert_status(self.client.post(path, headers=headers, json={
            **no_cases, "autofail": True, "autofail_reason": "Not applicable",
        }), 422)
        self.assert_status(self.client.post(f"{path}/score-preview", headers=headers, json={
            **no_cases, "autofail": True, "raw_scorecard": {},
        }), 400)
        self.assert_status(self.client.patch(delegated_path, headers=headers, json={
            "case_type": "No Cases", "autofail": True,
        }), 422)
        unscored = self.assert_status(self.client.patch(delegated_path, headers=headers, json={
            "case_type": "No Cases",
        }), 200).json()
        self.assertFalse(unscored["autofail"])
        self.assertIsNone(unscored["autofail_reason"])
        self.assertIsNone(unscored["scorecard_data"])
        self.assertIsNone(unscored["final_score"])
        scored = self.assert_status(self.client.patch(delegated_path, headers=headers, json={
            "case_type": "Initial Fix", "autofail": True, "autofail_reason": "Now scored.", "raw_scorecard": {},
        }), 200).json()
        self.assertEqual(scored["final_score"], 0)
        self.assertEqual(scored["scorecard_data"]["breakdown"], {})

    def test_autofail_metadata_patch_can_race_with_completion(self):
        agent = self.create_user(RoleEnum.SUPPORT)
        qa = self.create_user(RoleEnum.QA)
        headers = self.session(qa)
        draft = self.assert_status(self.client.post("/api/reviews", headers=headers, json={
            "support_agent_id": agent.id, "case_type": "Initial Fix", "save_for_later": True,
        }), 201).json()
        path = f"/api/reviews/{draft['id']}"
        start = Barrier(2, timeout=5)

        def edit(fields):
            start.wait()
            return self.client.patch(path, headers=headers, json=fields)

        with ThreadPoolExecutor(max_workers=2) as pool:
            for response in pool.map(edit, (
                {"autofail": True, "autofail_reason": "Critical handling mistake."},
                {"raw_scorecard": {"late_response": 5}},
            )):
                self.assert_status(response, 200)

        completed = self.assert_status(self.client.get(path, headers=headers), 200).json()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["final_score"], 0)
        self.assertTrue(completed["autofail"])
        self.assertEqual(completed["autofail_reason"], "Critical handling mistake.")
        self.assertEqual(completed["scorecard_data"]["total_penalty"], 5)

    def test_autofail_toggle_preserves_legacy_flat_breakdown(self):
        agent = self.create_user(RoleEnum.SUPPORT)
        qa = self.create_user(RoleEnum.QA)
        headers = self.session(qa)
        legacy = {"breakdown": {"deducted": 5, "multiplier": 1, "final_penalty": 5}}

        async def seed_legacy():
            async with async_session_factory() as db:
                review = Review(
                    support_agent_id=agent.id, qa_id=qa.id, case_type=CaseTypeEnum.INITIAL_FIX,
                    scorecard_data=legacy, final_score=95,
                )
                db.add(review)
                await db.commit()
                return review.id

        path = f"/api/reviews/{self.client.portal.call(seed_legacy)}"
        failed = self.assert_status(self.client.patch(path, headers=headers, json={
            "autofail": True, "autofail_reason": "Critical mistake.",
        }), 200).json()
        self.assertEqual(failed["final_score"], 0)
        self.assertEqual(failed["scorecard_data"], legacy)
        restored = self.assert_status(self.client.patch(path, headers=headers, json={
            "autofail": False,
        }), 200).json()
        self.assertEqual(restored["final_score"], 95)
        self.assertEqual(restored["scorecard_data"], legacy)

    def test_autofail_cannot_be_a_regular_scorecard_rule(self):
        agent = self.create_user(RoleEnum.SUPPORT)
        headers = self.session(self.create_user(RoleEnum.ADMIN), htmx=True)
        path = "/api/reviews"
        base = {"support_agent_id": agent.id, "case_type": "Initial Fix"}
        regular = self.assert_status(self.client.post(path, headers=headers, json=base), 201).json()
        for key in ("autofail", "Auto-fail", "auto_fail_2"):
            field = {"raw_scorecard": {key: 10}}
            with self.subTest(key=key):
                for url in (path, f"{path}/score-preview"):
                    self.assert_status(self.client.post(url, headers=headers, json={**base, **field}), 422)
                self.assert_status(self.client.patch(
                    f"{path}/{regular['id']}", headers=headers, json=field,
                ), 422)

        async def create_template():
            async with async_session_factory() as db:
                template = ScorecardTemplate(
                    name="Autofail validation fixture", case_type=CaseTypeEnum.INITIAL_FIX, is_active=False,
                    items=[ScorecardItem(error_name="valid_rule", display_name="Valid rule", penalty_points=5)],
                )
                db.add(template)
                await db.commit()
                return template.id, template.items[0].id

        async def remove_template(template_id):
            async with async_session_factory() as db:
                await db.execute(delete(ScorecardTemplate).where(ScorecardTemplate.id == template_id))
                await db.commit()

        template_id, item_id = self.client.portal.call(create_template)
        self.addCleanup(self.client.portal.call, remove_template, template_id)
        for item in ("", str(item_id)):
            rejected = self.assert_status(self.client.post(
                f"/admin/scorecards/{template_id}/save",
                headers=headers,
                data={
                    "name": "Autofail validation fixture", "case_type": "Initial Fix",
                    "item_id_0": item, "display_name_0": "Auto Fail", "penalty_0": "5", "active_0": "on",
                },
            ), 400)
            self.assertIn("separate review outcome", rejected.json()["detail"])

        # Validation comes after bulk-save may have staged other changes.
        self.assert_status(self.client.post(
            f"/admin/scorecards/{template_id}/save", headers=headers,
            data={
                "name": "Must not persist", "case_type": "Initial Fix", "removed_ids": str(item_id),
                "display_name_0": "Autofail", "penalty_0": "5", "active_0": "on",
            },
        ), 400)

        async def load_template():
            async with async_session_factory() as db:
                template = await db.get(ScorecardTemplate, template_id)
                item = await db.get(ScorecardItem, item_id)
                return template.name, item.display_name if item is not None else None

        self.assertEqual(self.client.portal.call(load_template), ("Autofail validation fixture", "Valid rule"))

    def test_review_delegation_completion_and_quota(self):
        async def scorecard():
            async with async_session_factory() as db:
                template = ScorecardTemplate(
                    name="CI Scorecard",
                    case_type=CaseTypeEnum.INITIAL_FIX,
                    items=[
                        ScorecardItem(
                            error_name="late_response", display_name="Late response", penalty_points=5
                        )
                    ],
                )
                db.add(template)
                await db.commit()
                return template.id

        async def remove_scorecard(template_id):
            async with async_session_factory() as db:
                await db.execute(delete(ScorecardTemplate).where(ScorecardTemplate.id == template_id))
                await db.commit()

        self.addCleanup(
            self.client.portal.call, remove_scorecard, self.client.portal.call(scorecard)
        )
        admin = self.session(self.create_user(RoleEnum.ADMIN))
        agent = self.create_user(RoleEnum.SUPPORT)
        support = self.session(agent)
        qa = self.create_user(RoleEnum.QA)
        payload = {
            "support_agent_id": agent.id,
            "case_type": "Initial Fix",
            "case_number": f"CI-{agent.id}",
        }
        pending_path = "/api/reviews/pending"
        self.assert_status(self.client.post(pending_path, headers=support, json=payload), 403)
        pending = self.assert_status(
            self.client.post(pending_path, headers=admin, json=payload), 201
        ).json()
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(pending["assigned_qa_id"])
        # Use the persisted creation time to avoid a UTC boundary race.
        _, _, year, month = reporting_period_for(datetime.fromisoformat(pending["created_at"]))
        quota_path = f"/api/reviews/quota/{agent.id}"
        period = {"year": year, "month": month}

        def completed_count():
            return self.assert_status(
                self.client.get(quota_path, headers=support, params=period), 200
            ).json()["completed"]

        self.assertEqual(completed_count(), 0)
        completed = self.assert_status(
            self.client.patch(
                f"/api/reviews/{pending['id']}", headers=self.session(qa),
                json={"raw_scorecard": {"late_response": 5}},
            ),
            200,
        ).json()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["qa_id"], qa.id)
        self.assertEqual(completed["final_score"], 95)
        self.assertEqual(completed["scorecard_data"]["rules_snapshot"]["case_type"], "Initial Fix")
        self.assertEqual(completed_count(), 1)

    def test_pending_review_duplicate_rule_across_write_paths(self):
        agent = self.create_user(RoleEnum.SUPPORT)
        admin = self.session(self.create_user(RoleEnum.ADMIN))
        qa = self.session(self.create_user(RoleEnum.QA))
        payload = {"support_agent_id": agent.id, "case_type": "Initial Fix", "case_number": "CI-open"}
        pending_path = "/api/reviews/pending"
        pending = self.assert_status(self.client.post(pending_path, headers=admin, json=payload), 201).json()
        self.assert_status(self.client.post(pending_path, headers=admin, json=payload), 409)
        self.assert_status(
            self.client.post("/api/reviews", headers=qa, json={
                **payload, "case_number": " CI-open ", "save_for_later": True,
            }),
            409,
        )
        bulk = self.assert_status(self.client.post(f"{pending_path}/bulk", headers=admin, json={
            "rows": [payload, {**payload, "case_number": "CI-other"}],
        }), 200).json()
        self.assertEqual(bulk["skipped"], [{"case_number": "CI-open", "reason": "already pending"}])
        other_path = f"/api/reviews/{bulk['created'][0]['id']}"
        original_path = f"/api/reviews/{pending['id']}"
        self.assert_status(self.client.patch(original_path, headers=qa, json={"notes": "Still open"}), 200)
        self.assert_status(self.client.patch(other_path, headers=qa, json={"case_number": " CI-open "}), 409)
        cleared = self.assert_status(
            self.client.patch(original_path, headers=qa, json={"case_number": None}), 200
        ).json()
        self.assertIsNone(cleared["case_number"])
        self.assert_status(self.client.patch(other_path, headers=qa, json={"case_number": "CI-open"}), 200)
        self.assert_status(self.client.delete(other_path, headers=qa), 204)
        self.assert_status(self.client.post(pending_path, headers=admin, json=payload), 201)

    def test_scorecard_snapshot_is_shared_without_changing_exemptions(self):
        agent = self.create_user(RoleEnum.SUPPORT)
        headers = self.session(self.create_user(RoleEnum.QA))
        raw_scorecard = {"late_response": 5}
        scoring = {"raw_scorecard": raw_scorecard}
        with patch("app.services.ai_service.analyze_support_ticket", new=AsyncMock(return_value=raw_scorecard)):
            auto = self.assert_status(self.client.post("/api/reviews/auto-score", headers=headers, json={
                "agent_id": agent.id, "case_type": "Initial Fix", "transcript": "A support case",
            }), 201).json()
        manual = self.assert_status(self.client.post("/api/reviews", headers=headers, json={
            "support_agent_id": agent.id, "case_type": "Initial Fix", **scoring,
            "no_multiplier_keys": ["late_response"],
        }), 201).json()
        expected = {
            "rules_snapshot": auto["scorecard_data"]["rules_snapshot"],
            "base_score": 100,
            "total_penalty": 5,
            "final_score": 95,
            "breakdown": {"late_response": {"deducted": 5, "multiplier": 1, "final_penalty": 5}},
        }
        self.assertEqual(expected["rules_snapshot"]["case_type"], "Initial Fix")
        self.assertEqual(auto["scorecard_data"], expected)
        manual_snapshot = {**expected, "multiplier_exemptions": ["late_response"]}
        self.assertEqual(manual["scorecard_data"], manual_snapshot)
        path = f"/api/reviews/{manual['id']}"
        rescored = self.assert_status(self.client.patch(path, headers=headers, json=scoring), 200).json()
        self.assertEqual(rescored["scorecard_data"], manual_snapshot)
        rescored = self.assert_status(
            self.client.patch(path, headers=headers, json={**scoring, "no_multiplier_keys": []}), 200
        ).json()
        self.assertEqual(rescored["scorecard_data"]["multiplier_exemptions"], [])
        self.assertEqual(rescored["scorecard_data"]["breakdown"]["late_response"]["multiplier"], 2)
        self.assertEqual(rescored["final_score"], 90)
