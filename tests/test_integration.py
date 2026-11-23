"""Exercise application contracts against a migrated PostgreSQL database."""

import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from threading import Barrier
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from openpyxl import Workbook
from sqlalchemy import delete, update

from app.db.database import async_session_factory, engine
from app.main import app
from app.models import CaseTypeEnum, RefundCheck, ReviewStatusEnum, RoleEnum, User
from app.models.scorecard import ScorecardItem, ScorecardTemplate
from app.services.reporting_period import reporting_period_for
from app.services.tracker_fields import archive_field, create_field, list_fields
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
        self.assertIn("/api/refund-check/import/inspect", schema["paths"])

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
        self.assertEqual(feedback["field_values"]["customer_feedback"], "CI feedback")
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

    def test_refund_check_fields_lifecycle_and_excel_are_isolated(self):
        admin, qa, support = map(self.session, (self.admin, self.qa, self.support))
        admin_hx = {**admin, "HX-Request": "true"}
        fields_path = "/admin/case-fields/refund-check"
        refund_api = "/api/refund-check"
        feedback_api = "/api/bad-feedback"

        async def active_fields(tracker):
            async with async_session_factory() as db:
                return {field.key: field for field in await list_fields(db, tracker)}

        def fields(tracker):
            return self.client.portal.call(active_fields, tracker)

        refund_defaults = fields("refund-check")
        feedback_fields = fields("bad-feedback")
        self.assertTrue(
            {"r_date", "source", "customer_info", "customer_feedback", "related_case"}
            <= refund_defaults.keys()
        )
        self.assertNotIn("fb_date", refund_defaults)
        self.assertIn("fb_date", feedback_fields)
        self.assertNotIn("r_date", feedback_fields)
        self.assert_status(
            self.client.post(
                f"{fields_path}/{refund_defaults['r_date'].id}/update",
                headers=admin_hx, data={"label": "Date", "field_type": "text"},
            ),
            400,
        )
        self.assertEqual(refund_defaults["source"].label, feedback_fields["source"].label)
        self.assertNotEqual(refund_defaults["source"].id, feedback_fields["source"].id)
        self.assertFalse(refund_defaults["customer_info"].show_in_my_reviews)

        form = {"label": "Paid on", "field_type": "date", "position": 0}
        denied = self.assert_status(
            self.client.post(f"{fields_path}/create", headers={**qa, "HX-Request": "true"}, data=form),
            200,
        )
        self.assertEqual(denied.headers.get("HX-Redirect"), "/")
        result = self.assert_status(
            self.client.post(f"{fields_path}/create", headers=admin_hx, data=form), 200
        )
        self.assertIn("Paid on", result.text)
        field = next(
            field for field in fields("refund-check").values()
            if field.key not in refund_defaults and field.label == "Paid on"
        )
        key = field.key
        self.assertNotIn(key, fields("bad-feedback"))
        self.assertEqual(next(iter(fields("refund-check"))), key)
        field_path = f"{fields_path}/{field.id}"

        def change_field(action, data=None):
            return self.assert_status(
                self.client.post(f"{field_path}/{action}", headers=admin_hx, data=data), 200
            )

        self.addCleanup(change_field, "archive")
        change_field("update", {**form, "label": "Refund paid on", "position": 1})
        self.assertEqual(fields("refund-check")[key].label, "Refund paid on")
        self.assertEqual(list(fields("refund-check"))[:2], ["r_date", key])
        for tracker, expected in (("refund-check", True), ("bad-feedback", False)):
            editor = self.assert_status(
                self.client.get(f"/partials/{tracker}-editor", headers=qa), 200
            )
            self.assertEqual("Refund paid on" in editor.text, expected)

        self.assert_status(self.client.get(refund_api, headers=support), 403)
        self.assert_status(
            self.client.post(refund_api, headers=qa, json={"field_values": {key: "not a date"}}),
            400,
        )
        self.assert_status(
            self.client.post(feedback_api, headers=qa, json={"field_values": {key: "2026-10-07"}}),
            400,
        )
        for endpoint, wrong_key in ((refund_api, "fb_date"), (feedback_api, "r_date")):
            self.assert_status(
                self.client.post(
                    endpoint, headers=qa, json={"field_values": {wrong_key: "2026-10-06"}},
                ),
                400,
            )
        self.assert_status(
            self.client.post(refund_api, headers=qa, json={"field_values": {"source": "x" * 256}}),
            400,
        )
        payload = {
            "r_date": "2026-10-04",
            "field_values": {
                key: "2026-10-07",
                "related_case": "Refund CI example",
                "customer_info": "private-refund@example.com",
            },
            "assigned_qa_id": self.qa.id,
            "agents": [{"user_id": self.support.id, "kind": "Support"}],
        }
        record = self.assert_status(self.client.post(refund_api, headers=qa, json=payload), 201).json()
        record_path = f"{refund_api}/{record['id']}"
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["r_date"], "2026-10-04")
        self.assertEqual(record["field_values"]["r_date"], "2026-10-04")
        self.assertNotIn("fb_date", record)
        self.assertNotIn("fb_date", record["field_values"])
        self.assertEqual(record["field_values"][key], "2026-10-07")
        self.assertEqual(record["related_case"], "Refund CI example")

        for endpoint, identity in (
            ("qa-matrix", qa), ("all-reviews", qa), ("to-review", qa), ("my-reviews", support),
        ):
            for tracker, expected in (("refund-check", True), ("bad-feedback", False)):
                view = self.assert_status(
                    self.client.get(f"/partials/{endpoint}", params={"cat": tracker}, headers=identity),
                    200,
                )
                self.assertEqual("Refund CI example" in view.text, expected)

        def personal_view():
            return self.assert_status(
                self.client.get(
                    "/partials/my-reviews", params={"cat": "refund-check"}, headers=support
                ),
                200,
            ).text

        self.assertNotIn("private-refund@example.com", personal_view())
        self.assertNotIn("Refund paid on", personal_view())
        change_field(
            "update",
            {**form, "label": "Refund paid on", "position": 1, "show_in_my_reviews": "true"},
        )
        self.assertIn("Refund paid on", personal_view())

        change_field("archive")
        self.assertNotIn(key, fields("refund-check"))
        self.assertNotIn("Refund paid on", personal_view())
        self.assert_status(
            self.client.patch(record_path, headers=qa, json={"field_values": {key: "2026-10-08"}}),
            400,
        )
        change_field("restore")
        restored = self.assert_status(self.client.get(record_path, headers=qa), 200).json()
        self.assertEqual(restored["field_values"][key], "2026-10-07")
        edit = self.assert_status(
            self.client.patch(
                record_path, headers=qa,
                json={"field_values": {"source": "Queue correction"}, "assigned_qa_id": None},
            ),
            200,
        ).json()
        self.assertIsNone(edit["assigned_qa_id"])
        self.assertEqual(edit["field_values"][key], "2026-10-07")
        related_case = refund_defaults["related_case"]
        case_field_path = f"{fields_path}/{related_case.id}"
        self.assert_status(self.client.post(f"{case_field_path}/archive", headers=admin_hx), 200)
        try:
            self.assertNotIn("Refund CI example", personal_view())
            self.assert_status(
                self.client.patch(record_path, headers=qa, json={"related_case": "Flat bypass"}),
                400,
            )
        finally:
            self.assert_status(self.client.post(f"{case_field_path}/restore", headers=admin_hx), 200)
        self.assertIn("Refund CI example", personal_view())
        done = self.assert_status(self.client.post(f"{record_path}/complete", headers=qa), 200).json()
        self.assertEqual(done["status"], "completed")
        self.assertEqual(done["qa_id"], self.qa.id)

        self.assertIsNotNone(done["completed_at"])
        self.assert_status(self.client.delete(record_path, headers=qa), 204)
        self.assert_status(self.client.get(record_path, headers=qa), 404)
        deleted = self.assert_status(
            self.client.get("/admin/partials/deleted-refund-check", headers=admin), 200
        )
        self.assertIn("Refund CI example", deleted.text)
        self.assert_status(
            self.client.post(f"/admin/refund-check/{record['id']}/restore", headers=admin_hx), 200
        )
        self.assertEqual(
            self.assert_status(self.client.get(record_path, headers=qa), 200).json()["status"],
            "completed",
        )
        self.assert_status(self.client.delete(record_path, headers=qa), 204)

        workbook = Workbook()
        long_feedback = "x" * 2_501
        workbook.active.append(["Refund paid on", "Date", "Support agents", "Customer feedback"])
        workbook.active.append(["7.10.2026", "2026-10-05", self.support.email, long_feedback])
        with BytesIO() as buffer:
            workbook.save(buffer)
            workbook.close()
            upload = {"file": ("refunds.xlsx", buffer.getvalue())}
        preview = self.assert_status(
            self.client.post(f"{refund_api}/import/inspect", headers=qa, files=upload), 200
        ).json()
        self.assertEqual(preview["suggestions"][key], "Refund paid on")
        self.assertEqual(preview["suggestions"]["r_date"], "Date")
        self.assertNotIn("fb_date", preview["suggestions"])
        report = self.assert_status(
            self.client.post(
                f"{refund_api}/import", headers=qa, files=upload,
                data={"mapping": json.dumps(
                    {
                        key: "Refund paid on",
                        "r_date": "Date",
                        "support_agents": "Support agents",
                        "customer_feedback": "Customer feedback",
                    }
                )},
            ),
            200,
        ).json()
        self.assertEqual(report["created_count"], 1)
        imported_path = f"{refund_api}/{report['created'][0]['id']}"
        imported = self.assert_status(self.client.get(imported_path, headers=qa), 200).json()
        self.assertEqual(imported["status"], "pending")
        self.assertEqual(imported["field_values"][key], "2026-10-07")
        self.assertEqual(imported["field_values"]["r_date"], "2026-10-05")
        self.assertEqual(imported["field_values"]["customer_feedback"], long_feedback)
        self.assertEqual(imported["agents"][0]["user_id"], self.support.id)
        self.assert_status(self.client.delete(imported_path, headers=qa), 204)

    def test_admin_case_field_position_and_reorder_contract(self):
        admin = self.session(self.admin)
        admin_hx = {**admin, "HX-Request": "true"}
        path = "/admin/case-fields/refund-check"

        async def fields(tracker="refund-check"):
            async with async_session_factory() as db:
                return await list_fields(db, tracker)

        def reorder(ids, headers=admin):
            return self.client.post(
                f"{path}/reorder", headers=headers, data={"field_ids": json.dumps(ids)}
            )

        original = self.client.portal.call(fields)
        original_ids = [field.id for field in original]
        foreign_id = self.client.portal.call(fields, "bad-feedback")[0].id
        created = self.assert_status(
            self.client.post(
                f"{path}/create", headers=admin_hx, data={"label": "Order contract", "field_type": "text"}
            ),
            200,
        )
        self.assertIn('id="case-fields-refund-check"', created.text)
        self.assertNotIn('id="case-fields-bad-feedback"', created.text)
        field = self.client.portal.call(fields)[-1]
        self.assertEqual(field.label, "Order contract")
        self.addCleanup(reorder, original_ids)
        self.addCleanup(self.client.post, f"{path}/{field.id}/archive", headers=admin)
        changed = self.assert_status(
            self.client.post(
                f"{path}/{field.id}/update", headers=admin,
                data={"label": "Order contract updated", "field_type": "text"},
            ),
            303,
        )
        self.assertEqual(changed.headers["location"], "/admin#case-fields")
        self.assertEqual(self.client.portal.call(fields)[-1].id, field.id)
        new_order = [field.id, *original_ids]
        denied = self.assert_status(reorder(new_order, self.session(self.qa)), 303)
        self.assertEqual(denied.headers["location"], "/")
        self.assert_status(reorder(new_order), 204)
        self.assertEqual([field.id for field in self.client.portal.call(fields)], new_order)
        for invalid in (original_ids, [foreign_id, *original_ids], [original_ids[0], *original_ids]):
            self.assert_status(reorder(invalid), 400)
        self.assertEqual([field.id for field in self.client.portal.call(fields)], new_order)

    def test_refund_check_my_reviews_does_not_reveal_others_completion_months(self):
        async def completed_for_someone_else():
            async with async_session_factory() as db:
                record = RefundCheck(
                    related_case="Other tester's refund",
                    qa_id=self.qa.id,
                    status=ReviewStatusEnum.COMPLETED,
                    completed_at=datetime(2020, 1, 8, tzinfo=UTC),
                )
                db.add(record)
                await db.commit()
                return record.id

        record_id = self.client.portal.call(completed_for_someone_else)
        qa = self.session(self.qa)
        self.addCleanup(
            self.client.delete, f"/api/refund-check/{record_id}", headers=qa
        )
        personal = "/partials/my-reviews?cat=refund-check&month=2020-01"
        others = self.assert_status(self.client.get(personal, headers=self.session(self.support)), 200)
        self.assertNotIn("January 2020", others.text)
        self.assertNotIn("Other tester", others.text)
        own = self.assert_status(self.client.get(personal, headers=qa), 200)
        self.assertIn("January 2020", own.text)
        self.assertIn("Other tester", own.text)

    def test_refund_check_concurrent_patches_keep_disjoint_custom_values(self):
        tracker = "refund-check"
        qa = self.session(self.qa)

        async def make_fields():
            async with async_session_factory() as db:
                fields = [
                    await create_field(db, tracker, label=label, field_type="text", position=100)
                    for label in ("Supplier", "Resolution")
                ]
                await db.commit()
                return fields

        async def archive_fields(fields):
            async with async_session_factory() as db:
                for field in fields:
                    await archive_field(db, tracker, field.id)
                await db.commit()

        fields = self.client.portal.call(make_fields)
        self.addCleanup(self.client.portal.call, archive_fields, fields)
        record = self.assert_status(
            self.client.post("/api/refund-check", headers=qa, json={}), 201
        ).json()
        self.assertEqual(record["agents"], [])
        path = f"/api/refund-check/{record['id']}"
        self.addCleanup(self.client.delete, path, headers=qa)
        expected = {fields[0].key: "Seller", fields[1].key: "Accepted"}
        start = Barrier(2, timeout=5)

        def edit(item):
            start.wait()
            return self.client.patch(path, headers=qa, json={"field_values": dict([item])})

        with ThreadPoolExecutor(max_workers=2) as pool:
            for response in pool.map(edit, expected.items()):
                self.assert_status(response, 200)
        saved = self.assert_status(self.client.get(path, headers=qa), 200).json()["field_values"]
        self.assertEqual({key: saved[key] for key in expected}, expected)

    def test_ai_is_unavailable_without_a_key(self):
        self.assert_status(
            self.client.post(
                "/api/ai/refactor", headers=self.session(self.qa), json={"html": "<p>CI</p>"}
            ),
            503,
        )
