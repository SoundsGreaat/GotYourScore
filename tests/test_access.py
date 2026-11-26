"""Authentication and endpoint access contracts."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from sqlalchemy import update

from app.db.database import async_session_factory
from app.models import RoleEnum, User
from tests.fixtures import PostgresTestCase


class AccessTests(PostgresTestCase):
    def test_assignment_conflict_detail_does_not_expose_user_names(self):
        supervisor = self.session(self.create_user(RoleEnum.SUPERVISOR))
        agent = self.create_user(RoleEnum.SUPPORT)
        first_qa = self.create_user(RoleEnum.QA)
        second_qa = self.create_user(RoleEnum.QA)
        created = self.assert_status(
            self.client.post(
                "/api/assignments",
                headers=supervisor,
                json={"qa_id": first_qa.id, "support_agent_id": agent.id},
            ),
            201,
        ).json()

        conflict = self.assert_status(
            self.client.post(
                "/api/assignments",
                headers=supervisor,
                json={"qa_id": second_qa.id, "support_agent_id": agent.id},
            ),
            409,
        ).json()
        self.assertEqual(conflict["detail"]["assignment_id"], created["id"])
        self.assertEqual(
            conflict["detail"]["message"], "This support agent is already assigned."
        )
        self.assertNotIn(agent.nickname, conflict["detail"]["message"])

    def test_anonymous_requests_use_transport_specific_login_responses(self):
        page = self.assert_status(self.client.get("/"), 303)
        self.assertEqual(page.headers["location"], "/login")
        self.assert_status(self.client.get("/api/reviews/pending/mine-count"), 401)
        partial = self.assert_status(
            self.client.get("/partials/my-reviews", headers={"HX-Request": "true"}), 200
        )
        self.assertEqual(partial.headers["HX-Redirect"], "/login")

    def test_google_callback_issues_a_real_session(self):
        email = f"oauth-{uuid4().hex}@example.com"
        userinfo = {"email": "outside@other.example", "email_verified": True, "name": "CI OAuth"}
        token_exchange = AsyncMock(return_value={"userinfo": userinfo})
        oauth = SimpleNamespace(google=SimpleNamespace(authorize_access_token=token_exchange))
        with patch("app.api.auth.get_oauth", return_value=oauth):
            self.assert_status(self.client.get("/auth/callback"), 403)
            userinfo["email"] = email
            self.addCleanup(self.client.cookies.clear)
            response = self.assert_status(self.client.get("/auth/callback"), 303)
            self.assertEqual(response.headers["location"], "/")
            self.assertIn("httponly", response.headers["set-cookie"].lower())
            me = self.assert_status(self.client.get("/auth/me"), 200).json()
            self.assertEqual(me["email"], email)
            self.assertEqual(me["roles"], ["Support"])

    def test_revoked_user_session_is_rejected(self):
        user = self.create_user(RoleEnum.SUPPORT)
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
        user = self.create_user(RoleEnum.ADMIN, RoleEnum.SUPPORT)
        headers = self.session(user, htmx=True)
        url = f"/admin/users/{user.id}/roles"
        partial = self.assert_status(self.client.get("/admin/partials/users", headers=headers), 200)
        self.assertRegex(partial.text, r'value="Admin"[^>]*\bchecked\s+disabled\b')
        self.assert_status(
            self.client.post(url, data={"roles": ["QA", "Support"]}, headers=headers), 200
        )
        me = self.assert_status(self.client.get("/auth/me", headers=headers), 200).json()
        self.assertCountEqual(me["roles"], ["Admin", "QA", "Support"])

        # Unchecking all editable roles submits no roles.
        self.assert_status(self.client.post(url, headers=headers), 200)
        me = self.assert_status(self.client.get("/auth/me", headers=headers), 200).json()
        self.assertEqual(me["roles"], ["Admin"])

        self.assert_status(self.client.post(url, data={"roles": "Unknown"}, headers=headers), 400)
        support = self.create_user(RoleEnum.SUPPORT)
        self.assert_status(
            self.client.post(f"/admin/users/{support.id}/roles", headers=headers), 400
        )

    def test_ai_is_unavailable_without_a_key(self):
        qa = self.session(self.create_user(RoleEnum.QA))
        self.assert_status(
            self.client.post("/api/ai/refactor", headers=qa, json={"html": "<p>CI</p>"}), 503
        )

    def test_validation_error_does_not_echo_submitted_values(self):
        qa = self.session(self.create_user(RoleEnum.QA))
        secret_value = "private-case-payload-987"
        response = self.assert_status(
            self.client.post(
                "/api/ai/refactor",
                headers=qa,
                json={"html": [secret_value]},
            ),
            422,
        )
        self.assertNotIn(secret_value, response.text)
        self.assertNotIn('"input"', response.text)
        self.assertEqual(response.json()["detail"][0]["msg"], "Invalid request.")
