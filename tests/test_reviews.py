"""Review lifecycle, duplicate prevention, and score persistence."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

from sqlalchemy import delete

from app.db.database import async_session_factory
from app.models import CaseTypeEnum, RoleEnum
from app.models.scorecard import ScorecardItem, ScorecardTemplate
from app.services.reporting_period import reporting_period_for
from tests.fixtures import PostgresTestCase


class ReviewTests(PostgresTestCase):
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
