"""Audit coverage for browser-style administrative authorization."""

from app.core.observability import JsonFormatter
from app.models import RoleEnum
from tests.fixtures import PostgresTestCase


class AdminAccessLogTests(PostgresTestCase):
    def test_role_change_records_actor_and_target(self):
        actor = self.create_user(RoleEnum.ADMIN)
        target = self.create_user(RoleEnum.SUPPORT)
        with self.assertLogs("app", "INFO") as captured:
            response = self.assert_status(self.client.post(
                f"/admin/users/{target.id}/roles", headers=self.session(actor, htmx=True),
                data={"roles": RoleEnum.QA.value},
            ), 200)
        event, = [record for record in captured.records if record.event == "admin.mutation.committed"]
        self.assertEqual((event.user_id, event.entity_id), (actor.id, target.id))
        self.assertEqual(event.request_id, response.headers["X-Request-ID"])

    def test_admin_htmx_permission_denial_is_logged_despite_http_200(self):
        actor = self.create_user(RoleEnum.SUPPORT)
        with self.assertLogs("app", "INFO") as captured:
            response = self.assert_status(self.client.post(
                f"/admin/users/{actor.id}/roles", headers=self.session(actor, htmx=True),
            ), 200)
        self.assertEqual(response.headers["HX-Redirect"], "/")
        event, = [record for record in captured.records if record.event == "authz.admin_denied"]
        self.assertEqual((event.user_id, event.reason_code), (actor.id, "insufficient_role"))
        self.assertEqual(event.request_id, response.headers["X-Request-ID"])
        summary, = [record for record in captured.records if record.event == "http.request.completed"]
        self.assertEqual((summary.outcome, summary.status_code), ("redirect", 200))

    def test_prompt_creation_records_the_committed_version_without_content(self):
        actor = self.create_user(RoleEnum.ADMIN)
        headers = self.session(actor, htmx=True)
        with self.assertLogs("app", "INFO") as captured:
            response = self.assert_status(self.client.post(
                "/admin/prompts", headers=headers,
                data={"key": "notes_refactor", "content": "private prompt body"},
            ), 200)
        event, = [record for record in captured.records if record.event == "admin.mutation.committed"]
        self.addCleanup(self.client.delete, f"/api/system-prompts/{event.entity_id}", headers=headers)
        self.assertEqual(event.operation, "admin_create_prompt")
        self.assertEqual(event.user_id, actor.id)
        self.assertGreater(event.entity_id, 0)
        self.assertEqual(event.request_id, response.headers["X-Request-ID"])
        self.assertNotIn("private prompt body", JsonFormatter().format(event))
