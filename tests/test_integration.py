"""Exercise application contracts against a migrated PostgreSQL database."""

import unittest
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import delete, update

from app.db.database import async_session_factory, engine
from app.main import app
from app.models import CaseTypeEnum, RoleEnum, User
from app.models.scorecard import ScorecardItem, ScorecardTemplate
from app.services.reporting_period import reporting_period_for
from tests.fixtures import create_user, require_test_database, session_cookie


class ApplicationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_test_database()
        cls.client = cls.enterClassContext(TestClient(app, follow_redirects=False))
        cls.addClassCleanup(cls.client.portal.call, engine.dispose)
        cls.admin = cls.client.portal.call(create_user, RoleEnum.ADMIN)
        cls.qa = cls.client.portal.call(create_user, RoleEnum.QA)
        cls.support = cls.client.portal.call(create_user, RoleEnum.SUPPORT)

    def session(self, user):
        return {"Cookie": f"session={session_cookie(user.id)}"}

    def assert_status(self, response, status):
        self.assertEqual(response.status_code, status, response.text[:1000])
        return response

    def test_startup_routes_and_openapi(self):
        self.assertEqual(self.client.get("/ping").json()["status"], "ok")
        login = self.assert_status(self.client.get("/"), 303)
        self.assertEqual(login.headers["location"], "/login")
        self.assertIn("Login with Google", self.client.get("/login").text)
        self.assert_status(self.client.get("/api/reviews/pending/mine-count"), 401)
        partial = self.assert_status(
            self.client.get("/partials/my-reviews", headers={"HX-Request": "true"}), 200
        )
        self.assertEqual(partial.headers["HX-Redirect"], "/login")
        schema = self.assert_status(self.client.get("/openapi.json"), 200).json()
        self.assertIn("/api/bad-feedback/import/inspect", schema["paths"])

    def test_google_callback_issues_a_real_session(self):
        email = f"oauth-{self.support.id}@example.com"
        userinfo = {"email": email, "email_verified": True, "name": "CI OAuth"}
        token_exchange = AsyncMock(return_value={"userinfo": userinfo})
        oauth = SimpleNamespace(google=SimpleNamespace(authorize_access_token=token_exchange))
        with patch("app.api.auth.get_oauth", return_value=oauth):
            userinfo["email"] = "outside@other.example"
            self.assert_status(self.client.get("/auth/callback"), 403)
            userinfo["email"] = email
            try:
                response = self.assert_status(self.client.get("/auth/callback"), 303)
                self.assertEqual(response.headers["location"], "/")
                self.assertIn("httponly", response.headers["set-cookie"].lower())
                me = self.assert_status(self.client.get("/auth/me"), 200).json()
                self.assertEqual(me["email"], email)
                self.assertEqual(me["roles"], ["Support"])
            finally:
                self.client.cookies.clear()

    def test_revoked_user_session_is_rejected(self):
        user = self.client.portal.call(create_user, RoleEnum.SUPPORT)
        headers = self.session(user)
        self.assert_status(self.client.get("/auth/me", headers=headers), 200)

        async def soft_delete():
            async with async_session_factory() as db:
                await db.execute(
                    update(User).where(User.id == user.id).values(deleted_at=datetime.now(UTC))
                )
                await db.commit()

        self.client.portal.call(soft_delete)
        self.assert_status(self.client.get("/auth/me", headers=headers), 401)

    def test_admin_can_edit_own_other_roles_without_losing_admin(self):
        user = self.client.portal.call(create_user, RoleEnum.ADMIN, RoleEnum.SUPPORT)
        headers = {**self.session(user), "HX-Request": "true"}
        url = f"/admin/users/{user.id}/roles"
        partial = self.assert_status(self.client.get("/admin/partials/users", headers=headers), 200)
        self.assertRegex(partial.text, r'value="Admin"[^>]*\bchecked\s+disabled\b')

        page = self.assert_status(
            self.client.post(url, data={"roles": ["QA", "Support"]}, headers=headers), 200
        )
        self.assertIn("Roles updated", page.text)
        me = self.assert_status(self.client.get("/auth/me", headers=headers), 200).json()
        self.assertCountEqual(me["roles"], ["Admin", "QA", "Support"])

        # Unchecking all editable roles sends no roles at all.
        self.assert_status(self.client.post(url, headers=headers), 200)
        me = self.assert_status(self.client.get("/auth/me", headers=headers), 200).json()
        self.assertEqual(me["roles"], ["Admin"])

        self.assert_status(self.client.post(url, data={"roles": "Unknown"}, headers=headers), 400)
        self.assert_status(
            self.client.post(f"/admin/users/{self.support.id}/roles", headers=headers), 400
        )

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
        agent_id = self.support.id
        admin, qa, support = map(self.session, (self.admin, self.qa, self.support))
        quota_path = f"/api/reviews/quota/{agent_id}"
        # Use an explicit period on both reads to avoid a date-boundary race.
        _, _, year, month = reporting_period_for(datetime.now(UTC))
        period = {"year": year, "month": month}
        payload = {
            "support_agent_id": agent_id,
            "case_type": "Initial Fix",
            "case_number": f"CI-{agent_id}",
        }
        pending_path = "/api/reviews/pending"
        self.assert_status(self.client.post(pending_path, headers=support, json=payload), 403)
        pending = self.assert_status(
            self.client.post(pending_path, headers=admin, json=payload), 201
        ).json()
        self.assertEqual(pending["status"], "pending")
        self.assertIsNone(pending["assigned_qa_id"])
        self.assert_status(self.client.post(pending_path, headers=admin, json=payload), 409)
        baseline = self.assert_status(
            self.client.get(quota_path, headers=support, params=period), 200
        ).json()["completed"]
        self.assertEqual(baseline, 0)
        review_path = f"/api/reviews/{pending['id']}"
        completed = self.assert_status(
            self.client.patch(review_path, headers=qa, json={"raw_scorecard": {"late_response": 5}}),
            200,
        ).json()
        self.assertEqual(completed["status"], "completed")
        self.assertEqual(completed["qa_id"], self.qa.id)
        self.assertEqual(completed["final_score"], 95)
        self.assertIn("rules_snapshot", completed["scorecard_data"])
        self.assertEqual(
            self.client.get(quota_path, headers=support, params=period).json()["completed"], 1
        )

    def test_bad_feedback_lifecycle_and_excel_upload(self):
        headers = self.session(self.qa)
        feedback = self.assert_status(
            self.client.post(
                "/api/bad-feedback",
                headers=headers,
                json={
                    "customer_feedback": "CI feedback",
                    "agents": [{"user_id": self.support.id, "kind": "Support"}],
                },
            ),
            201,
        ).json()
        self.assertEqual(feedback["status"], "pending")
        path = f"/api/bad-feedback/{feedback['id']}"
        completed = self.assert_status(self.client.post(f"{path}/complete", headers=headers), 200)
        self.assertEqual(completed.json()["status"], "completed")
        self.assertIsNotNone(completed.json()["completed_at"])
        self.assert_status(self.client.get(path, headers=headers), 200)
        self.assert_status(self.client.delete(path, headers=headers), 204)
        self.assert_status(self.client.get(path, headers=headers), 404)

        workbook = Workbook()
        workbook.active.append(["Related Case", "Customer Feedback"])
        workbook.active.append(["CI-001", "Excel sample"])
        with BytesIO() as data:
            workbook.save(data)
            workbook.close()
            result = self.assert_status(
                self.client.post(
                    "/api/bad-feedback/import/inspect",
                    headers=headers,
                    files={"file": ("feedback.xlsx", data.getvalue())},
                ),
                200,
            ).json()
        self.assertEqual(result["headers"], ["Related Case", "Customer Feedback"])
        self.assertEqual(result["preview"][0]["values"], ["CI-001", "Excel sample"])

    def test_ai_is_unavailable_without_a_key(self):
        self.assert_status(
            self.client.post(
                "/api/ai/refactor", headers=self.session(self.qa), json={"html": "<p>CI</p>"}
            ),
            503,
        )
