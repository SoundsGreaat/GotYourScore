"""Admin case-field configuration and its effects on saved records."""

import json
from uuid import uuid4

from app.db.database import async_session_factory
from app.models import RoleEnum
from app.services.tracker_fields import list_fields
from tests.fixtures import PostgresTestCase

REFUND_FIELDS = "/admin/case-fields/refund-check"
REFUND_API = "/api/refund-check"


class CaseFieldTests(PostgresTestCase):
    def fields(self, tracker="refund-check"):
        async def active_fields():
            async with async_session_factory() as db:
                return {field.key: field for field in await list_fields(db, tracker)}

        return self.client.portal.call(active_fields)

    def my_refunds(self, user):
        return self.assert_status(
            self.client.get(
                "/partials/my-reviews", params={"cat": "refund-check"}, headers=self.session(user)
            ),
            200,
        ).text

    def test_admin_case_field_position_and_reorder_contract(self):
        admin = self.create_user(RoleEnum.ADMIN)
        admin_headers = self.session(admin)
        qa = self.session(self.create_user(RoleEnum.QA))
        original = self.fields()
        original_ids = [field.id for field in original.values()]
        foreign_id = next(iter(self.fields("bad-feedback").values())).id

        def reorder(ids, headers=admin_headers):
            return self.client.post(
                f"{REFUND_FIELDS}/reorder", headers=headers, data={"field_ids": json.dumps(ids)}
            )

        form = {"label": f"Order contract {uuid4().hex[:8]}", "field_type": "text"}
        created = self.assert_status(
            self.client.post(f"{REFUND_FIELDS}/create", headers=self.session(admin, htmx=True), data=form),
            200,
        )
        self.assertIn('id="case-fields-refund-check"', created.text)
        self.assertNotIn('id="case-fields-bad-feedback"', created.text)
        fields = self.fields()
        field = next(field for key, field in fields.items() if key not in original)
        field_path = f"{REFUND_FIELDS}/{field.id}"
        self.addCleanup(lambda: self.assert_status(reorder(original_ids), 204))
        self.addCleanup(
            lambda: self.assert_status(self.client.post(f"{field_path}/archive", headers=admin_headers), 303)
        )
        self.assertEqual(next(reversed(fields)), field.key)

        changed = self.assert_status(
            self.client.post(
                f"{field_path}/update", headers=admin_headers,
                data={**form, "label": "Order contract updated"},
            ),
            303,
        )
        self.assertEqual(changed.headers["location"], "/admin#case-fields")
        self.assertEqual(next(reversed(self.fields())), field.key)
        new_order = [field.id, *original_ids]
        denied = self.assert_status(reorder(new_order, qa), 303)
        self.assertEqual(denied.headers["location"], "/")
        self.assert_status(reorder(new_order), 204)
        self.assertEqual([field.id for field in self.fields().values()], new_order)
        for invalid in (original_ids, [foreign_id, *original_ids], [original_ids[0], *original_ids]):
            with self.subTest(field_ids=invalid):
                self.assert_status(reorder(invalid), 400)
        self.assertEqual([field.id for field in self.fields().values()], new_order)

    def test_custom_refund_field_visibility_archive_and_restore(self):
        admin = self.session(self.create_user(RoleEnum.ADMIN), htmx=True)
        qa_user = self.create_user(RoleEnum.QA)
        qa = self.session(qa_user)
        agent = self.create_user(RoleEnum.SUPPORT)
        defaults = self.fields()
        form = {"label": "Paid on", "field_type": "date", "position": 0}
        denied = self.assert_status(
            self.client.post(f"{REFUND_FIELDS}/create", headers=self.session(qa_user, htmx=True), data=form),
            200,
        )
        self.assertEqual(denied.headers["HX-Redirect"], "/")
        self.assert_status(self.client.post(f"{REFUND_FIELDS}/create", headers=admin, data=form), 200)
        field = next(field for key, field in self.fields().items() if key not in defaults)
        key = field.key
        field_path = f"{REFUND_FIELDS}/{field.id}"

        def change_field(action, data=None):
            return self.assert_status(self.client.post(f"{field_path}/{action}", headers=admin, data=data), 200)

        self.addCleanup(change_field, "archive")
        self.assertEqual(next(iter(self.fields())), key)
        label = "Refund paid on"
        updated = {**form, "label": label, "position": 1}
        change_field("update", updated)
        self.assertEqual(list(self.fields())[:2], ["r_date", key])
        for tracker in ("refund-check", "bad-feedback"):
            with self.subTest(tracker=tracker):
                editor = self.assert_status(
                    self.client.get(f"/partials/{tracker}-editor", headers=qa), 200
                )
                self.assertEqual(label in editor.text, tracker == "refund-check")

        self.assert_status(
            self.client.post(REFUND_API, headers=qa, json={"field_values": {key: "not a date"}}), 400
        )
        values = {key: "2026-10-07", "customer_info": "private-refund@example.com"}
        self.assert_status(
            self.client.post("/api/bad-feedback", headers=qa, json={"field_values": {key: values[key]}}),
            400,
        )
        record = self.assert_status(
            self.client.post(REFUND_API, headers=qa, json={
                "field_values": values, "agents": [{"user_id": agent.id, "kind": "Support"}],
            }),
            201,
        ).json()
        record_path = f"{REFUND_API}/{record['id']}"
        self.addCleanup(self.client.delete, record_path, headers=qa)

        view = self.my_refunds(agent)
        self.assertNotIn(values["customer_info"], view)
        self.assertNotIn(label, view)
        change_field("update", {**updated, "show_in_my_reviews": "true"})
        self.assertIn(label, self.my_refunds(agent))

        change_field("archive")
        self.assertNotIn(label, self.my_refunds(agent))
        self.assert_status(
            self.client.patch(record_path, headers=qa, json={"field_values": {key: "2026-10-08"}}), 400
        )
        change_field("restore")
        restored = self.assert_status(self.client.get(record_path, headers=qa), 200).json()
        self.assertEqual(restored["field_values"][key], values[key])

    def test_builtin_refund_date_cannot_change_type(self):
        admin = self.session(self.create_user(RoleEnum.ADMIN), htmx=True)
        date_field = self.fields()["r_date"]
        self.assert_status(
            self.client.post(
                f"{REFUND_FIELDS}/{date_field.id}/update",
                headers=admin, data={"label": "Date", "field_type": "text"},
            ),
            400,
        )

    def test_numeric_type_changes_validate_existing_values_without_rewriting_them(self):
        admin = self.session(self.create_user(RoleEnum.ADMIN), htmx=True)
        qa = self.session(self.create_user(RoleEnum.QA))
        label = f"Existing case data {uuid4().hex[:8]}"
        self.assert_status(
            self.client.post(
                f"{REFUND_FIELDS}/create", headers=admin,
                data={"label": label, "field_type": "text"},
            ),
            200,
        )
        field = next(field for field in self.fields().values() if field.label == label)
        field_path = f"{REFUND_FIELDS}/{field.id}"
        self.addCleanup(self.client.post, f"{field_path}/archive", headers=admin)
        key = field.key
        record = self.assert_status(
            self.client.post(REFUND_API, headers=qa, json={"field_values": {key: "Unknown"}}),
            201,
        ).json()
        record_path = f"{REFUND_API}/{record['id']}"
        self.addCleanup(self.client.delete, record_path, headers=qa)

        def change_type(field_type, expected_status):
            self.assert_status(
                self.client.post(
                    f"{field_path}/update", headers=admin,
                    data={"label": label, "field_type": field_type},
                ),
                expected_status,
            )

        change_type("decimal", 400)
        self.assertEqual(self.fields()[key].field_type, "text")
        for value, compatible_type in ((" 0004.25 ", "decimal"), ("0004.000", "integer")):
            self.assert_status(
                self.client.patch(record_path, headers=qa, json={"field_values": {key: value}}),
                200,
            )
            original = self.assert_status(
                self.client.get(record_path, headers=qa), 200
            ).json()["field_values"][key]
            change_type(compatible_type, 200)
            self.assertEqual(self.fields()[key].field_type, compatible_type)
            stored = self.assert_status(self.client.get(record_path, headers=qa), 200).json()
            self.assertEqual(stored["field_values"][key], original)
            if compatible_type == "decimal":
                self.assertEqual(original, value)
                change_type("integer", 400)
                self.assertEqual(self.fields()[key].field_type, "decimal")

    def test_archived_builtin_field_hides_saved_values_and_rejects_flat_writes(self):
        admin = self.session(self.create_user(RoleEnum.ADMIN), htmx=True)
        qa = self.session(self.create_user(RoleEnum.QA))
        agent = self.create_user(RoleEnum.SUPPORT)
        marker = f"Archive contract {uuid4().hex[:8]}"
        record = self.assert_status(
            self.client.post(REFUND_API, headers=qa, json={
                "related_case": marker, "agents": [{"user_id": agent.id, "kind": "Support"}],
            }),
            201,
        ).json()
        record_path = f"{REFUND_API}/{record['id']}"
        self.addCleanup(self.client.delete, record_path, headers=qa)

        field_path = f"{REFUND_FIELDS}/{self.fields()['related_case'].id}"
        self.assert_status(self.client.post(f"{field_path}/archive", headers=admin), 200)
        try:
            self.assertNotIn(marker, self.my_refunds(agent))
            self.assert_status(
                self.client.patch(record_path, headers=qa, json={"related_case": "Flat bypass"}), 400
            )
        finally:
            self.assert_status(self.client.post(f"{field_path}/restore", headers=admin), 200)
        self.assertIn(marker, self.my_refunds(agent))
