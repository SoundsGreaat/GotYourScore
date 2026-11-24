"""Tracker API contracts for lifecycle, privacy, and Excel import."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from threading import Barrier
from uuid import uuid4

from openpyxl import Workbook

from app.db.database import async_session_factory
from app.models import RefundCheck, ReviewStatusEnum, RoleEnum
from app.services.tracker_fields import archive_field, create_field
from tests.fixtures import PostgresTestCase

REFUND = "/api/refund-check"
FEEDBACK = "/api/bad-feedback"


class TrackerTests(PostgresTestCase):
    def refund_fields(self, *labels, field_type="text"):
        async def create():
            async with async_session_factory() as db:
                fields = [
                    await create_field(db, "refund-check", label=label, field_type=field_type)
                    for label in labels
                ]
                await db.commit()
                return fields

        async def archive(fields):
            async with async_session_factory() as db:
                for field in fields:
                    await archive_field(db, "refund-check", field.id)
                await db.commit()

        fields = self.client.portal.call(create)
        self.addCleanup(self.client.portal.call, archive, fields)
        return fields

    def test_tracker_lifecycle_respects_tracker_specific_date_keys(self):
        qa_user = self.create_user(RoleEnum.QA)
        qa = self.session(qa_user)
        for endpoint, date_key, other_key in (
            (REFUND, "r_date", "fb_date"),
            (FEEDBACK, "fb_date", "r_date"),
        ):
            with self.subTest(endpoint=endpoint):
                self.assert_status(
                    self.client.post(endpoint, headers=qa, json={"field_values": {other_key: "2026-10-06"}}),
                    400,
                )
                record = self.assert_status(
                    self.client.post(endpoint, headers=qa, json={date_key: "2026-10-04"}), 201
                ).json()
                path = f"{endpoint}/{record['id']}"
                self.addCleanup(self.client.delete, path, headers=qa)
                self.assertEqual(record["status"], "pending")
                self.assertEqual(record[date_key], "2026-10-04")
                self.assertEqual(record["field_values"][date_key], "2026-10-04")
                self.assertNotIn(other_key, record["field_values"])
                done = self.assert_status(self.client.post(f"{path}/complete", headers=qa), 200).json()
                self.assertEqual(done["status"], "completed")
                self.assertEqual(done["qa_id"], qa_user.id)
                self.assertIsNotNone(done["completed_at"])
                self.assert_status(self.client.delete(path, headers=qa), 204)
                self.assert_status(self.client.get(path, headers=qa), 404)

    def test_refund_pending_edits_stay_in_category_and_record_can_be_restored(self):
        qa_user = self.create_user(RoleEnum.QA)
        qa = self.session(qa_user)
        admin = self.session(self.create_user(RoleEnum.ADMIN), htmx=True)
        support = self.session(self.create_user(RoleEnum.SUPPORT))
        self.assert_status(self.client.get(REFUND, headers=support), 403)
        self.assert_status(
            self.client.post(REFUND, headers=qa, json={"field_values": {"source": "x" * 256}}), 400
        )

        marker = f"Refund CI {uuid4().hex[:8]}"
        record = self.assert_status(
            self.client.post(REFUND, headers=qa, json={
                "related_case": marker, "assigned_qa_id": qa_user.id,
            }),
            201,
        ).json()
        path = f"{REFUND}/{record['id']}"
        self.addCleanup(self.client.delete, path, headers=qa)
        edited = self.assert_status(
            self.client.patch(
                path, headers=qa,
                json={"field_values": {"source": "Queue correction"}, "assigned_qa_id": None},
            ),
            200,
        ).json()
        self.assertIsNone(edited["assigned_qa_id"])
        self.assertEqual(edited["status"], "pending")
        self.assertEqual(edited["field_values"]["related_case"], marker)

        for tracker in ("refund-check", "bad-feedback"):
            with self.subTest(tracker=tracker):
                view = self.assert_status(
                    self.client.get("/partials/all-reviews", params={"cat": tracker}, headers=qa), 200
                )
                self.assertEqual(marker in view.text, tracker == "refund-check")

        self.assert_status(self.client.post(f"{path}/complete", headers=qa), 200)
        self.assert_status(self.client.delete(path, headers=qa), 204)
        deleted = self.assert_status(self.client.get("/admin/partials/deleted-refund-check", headers=admin), 200)
        self.assertIn(marker, deleted.text)
        self.assert_status(
            self.client.post(f"/admin/refund-check/{record['id']}/restore", headers=admin), 200
        )
        self.assertEqual(self.assert_status(self.client.get(path, headers=qa), 200).json()["status"], "completed")

    def test_excel_import_resolves_refund_fields_dates_agents_and_long_feedback(self):
        qa = self.session(self.create_user(RoleEnum.QA))
        agent = self.create_user(RoleEnum.SUPPORT)
        field, = self.refund_fields("Refund paid on", field_type="date")
        long_feedback = "x" * 2_501
        workbook = Workbook()
        self.addCleanup(workbook.close)
        columns = [field.label, "Date", "Support agents", "Customer feedback"]
        row = ["7.10.2026", "2026-10-05", agent.email, long_feedback]
        workbook.active.append(columns)
        workbook.active.append(row)
        with BytesIO() as buffer:
            workbook.save(buffer)
            upload = {"file": ("refunds.xlsx", buffer.getvalue())}

        for endpoint, date_key, other_key in (
            (REFUND, "r_date", "fb_date"),
            (FEEDBACK, "fb_date", "r_date"),
        ):
            with self.subTest(endpoint=endpoint):
                preview = self.assert_status(
                    self.client.post(f"{endpoint}/import/inspect", headers=qa, files=upload), 200
                ).json()
                self.assertEqual(preview["headers"], columns)
                self.assertEqual(preview["preview"][0]["values"][:3], row[:3])
                suggestions = preview["suggestions"]
                self.assertEqual(suggestions[date_key], "Date")
                self.assertNotIn(other_key, suggestions)
                self.assertEqual(suggestions.get(field.key), field.label if endpoint == REFUND else None)
        mapping = {
            field.key: field.label,
            "r_date": "Date",
            "support_agents": "Support agents",
            "customer_feedback": "Customer feedback",
        }
        report = self.assert_status(
            self.client.post(
                f"{REFUND}/import", headers=qa, files=upload, data={"mapping": json.dumps(mapping)}
            ),
            200,
        ).json()
        self.assertEqual(report["created_count"], 1)
        path = f"{REFUND}/{report['created'][0]['id']}"
        self.addCleanup(self.client.delete, path, headers=qa)
        imported = self.assert_status(self.client.get(path, headers=qa), 200).json()
        self.assertEqual(imported["status"], "pending")
        self.assertEqual(
            {key: imported["field_values"][key] for key in (field.key, "r_date", "customer_feedback")},
            {field.key: "2026-10-07", "r_date": "2026-10-05", "customer_feedback": long_feedback},
        )
        self.assertEqual([agent_data["user_id"] for agent_data in imported["agents"]], [agent.id])

    def test_refund_my_reviews_does_not_reveal_others_completion_months(self):
        qa = self.create_user(RoleEnum.QA)
        support = self.create_user(RoleEnum.SUPPORT)

        async def completed_for_someone_else():
            async with async_session_factory() as db:
                record = RefundCheck(
                    related_case="Other tester's refund",
                    qa_id=qa.id,
                    status=ReviewStatusEnum.COMPLETED,
                    completed_at=datetime(2020, 1, 8, tzinfo=UTC),
                )
                db.add(record)
                await db.commit()
                return record.id

        record_id = self.client.portal.call(completed_for_someone_else)
        self.addCleanup(self.client.delete, f"{REFUND}/{record_id}", headers=self.session(qa))
        personal = "/partials/my-reviews?cat=refund-check&month=2020-01"
        others = self.assert_status(self.client.get(personal, headers=self.session(support)), 200)
        self.assertNotIn("January 2020", others.text)
        self.assertNotIn("Other tester", others.text)
        own = self.assert_status(self.client.get(personal, headers=self.session(qa)), 200)
        self.assertIn("January 2020", own.text)
        self.assertIn("Other tester", own.text)

    def test_refund_concurrent_patches_keep_disjoint_custom_values(self):
        qa = self.session(self.create_user(RoleEnum.QA))
        supplier, resolution = self.refund_fields("Supplier", "Resolution")
        record = self.assert_status(self.client.post(REFUND, headers=qa, json={}), 201).json()
        path = f"{REFUND}/{record['id']}"
        self.addCleanup(self.client.delete, path, headers=qa)
        expected = {supplier.key: "Seller", resolution.key: "Accepted"}
        start = Barrier(2, timeout=5)

        def edit(item):
            key, value = item
            start.wait()
            return self.client.patch(path, headers=qa, json={"field_values": {key: value}})

        with ThreadPoolExecutor(max_workers=2) as pool:
            for response in pool.map(edit, expected.items()):
                self.assert_status(response, 200)
        saved = self.assert_status(self.client.get(path, headers=qa), 200).json()["field_values"]
        self.assertEqual({key: saved[key] for key in expected}, expected)
