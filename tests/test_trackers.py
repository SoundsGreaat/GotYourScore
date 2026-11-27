"""Tracker API contracts for lifecycle, privacy, and Excel import."""

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
from threading import Barrier
from uuid import uuid4
from xml.etree import ElementTree
from zipfile import ZipFile

from openpyxl import Workbook
from openpyxl.xml.constants import SHEET_MAIN_NS
from sqlalchemy import func, select

from app.core.observability import JsonFormatter
from app.db.database import async_session_factory
from app.models import RefundCheck, ReviewStatusEnum, RoleEnum, User
from app.services.tracker_fields import archive_field, create_field, list_fields
from tests.fixtures import PostgresTestCase

REFUND = "/api/refund-check"
FEEDBACK = "/api/bad-feedback"


def excel_upload(*rows, numeric_tokens=None):
    workbook = Workbook()
    try:
        for row in rows:
            workbook.active.append(row)
        with BytesIO() as buffer:
            workbook.save(buffer)
            content = buffer.getvalue()
    finally:
        workbook.close()
    if numeric_tokens:
        # The writer rounds native numbers itself, so set the exact OOXML
        # tokens after saving. Formula caches also have to be set in the XML.
        with ZipFile(BytesIO(content)) as source, BytesIO() as buffer:
            with ZipFile(buffer, "w") as target:
                for name in source.namelist():
                    data = source.read(name)
                    if name == "xl/worksheets/sheet1.xml":
                        sheet = ElementTree.fromstring(data)
                        for coordinate, value in numeric_tokens.items():
                            cell = sheet.find(f".//{{{SHEET_MAIN_NS}}}c[@r='{coordinate}']")
                            if value is None:  # A formula cached an empty string.
                                cell.set("t", "str")
                            cell.find(f"{{{SHEET_MAIN_NS}}}v").text = value
                        data = ElementTree.tostring(sheet)
                    target.writestr(name, data)
            content = buffer.getvalue()
    return {"file": ("tracker.xlsx", content)}


class TrackerTests(PostgresTestCase):
    def test_import_rejects_malformed_workbook_as_client_error(self):
        qa = self.session(self.create_user(RoleEnum.QA))
        response = self.assert_status(
            self.client.post(
                f"{FEEDBACK}/import/inspect",
                headers=qa,
                files={"file": ("invalid.xlsx", b"not-an-xlsx-workbook")},
            ),
            400,
        )
        self.assertEqual(
            response.json()["detail"],
            "Could not read the file as an .xlsx workbook.",
        )

    def case_fields(self, tracker, *labels, field_type="text"):
        async def create():
            async with async_session_factory() as db:
                fields = [
                    await create_field(db, tracker, label=label, field_type=field_type)
                    for label in labels
                ]
                await db.commit()
                return fields

        async def archive(fields):
            async with async_session_factory() as db:
                for field in fields:
                    await archive_field(db, tracker, field.id)
                await db.commit()

        fields = self.client.portal.call(create)
        self.addCleanup(self.client.portal.call, archive, fields)
        return fields

    def user_count(self):
        async def count():
            async with async_session_factory() as db:
                return await db.scalar(select(func.count()).select_from(User))

        return self.client.portal.call(count)

    def test_numeric_values_round_trip_exactly_and_reject_invalid_input(self):
        qa = self.session(self.create_user(RoleEnum.QA))
        json_headers = {**qa, "Content-Type": "application/json"}
        precise = "9007199254740993.123456789012345678900"
        for endpoint in (FEEDBACK, REFUND):
            with self.subTest(endpoint=endpoint):
                tracker = endpoint.rsplit("/", 1)[-1]
                quantity, = self.case_fields(tracker, "Quantity", field_type="integer")
                amount, = self.case_fields(tracker, "Amount", field_type="decimal")
                record = self.assert_status(
                    self.client.post(
                        endpoint,
                        headers=json_headers,
                        content=f'{{"field_values":{{"{quantity.key}":0,"{amount.key}":{precise}}}}}',
                    ),
                    201,
                ).json()
                path = f"{endpoint}/{record['id']}"
                self.addCleanup(self.client.delete, path, headers=qa)
                expected = {quantity.key: "0", amount.key: precise}
                self.assertEqual({key: record["field_values"][key] for key in expected}, expected)
                self.assertEqual(
                    self.assert_status(self.client.get(path, headers=qa), 200).json()["field_values"],
                    {**record["field_values"], **expected},
                )

                for field, invalid_token, error_code in (
                    (quantity, "1.000000000000000000001", "invalid_integer"),
                    (quantity, '"Infinity"', "invalid_integer"),
                    (amount, '"not a number"', "invalid_decimal"),
                    (amount, "NaN", "invalid_decimal"),
                ):
                    with self.subTest(field=field.label, invalid=invalid_token):
                        invalid = self.assert_status(
                            self.client.patch(
                                path,
                                headers=json_headers,
                                content=f'{{"field_values":{{"{field.key}":{invalid_token}}}}}',
                            ),
                            400,
                        ).json()
                        self.assertEqual(
                            invalid["detail"], {"code": error_code, "field_key": field.key}
                        )
                self.assert_status(
                    self.client.patch(path, headers=qa, json={"field_values": {amount.key: True}}),
                    422,
                )

                updated = self.assert_status(
                    self.client.patch(
                        path, headers=qa, json={"field_values": {quantity.key: "-23", amount.key: "  "}},
                    ),
                    200,
                ).json()
                self.assertEqual(updated["field_values"][quantity.key], "-23")
                self.assertIsNone(updated["field_values"].get(amount.key))
                stored = self.assert_status(self.client.get(path, headers=qa), 200).json()
                self.assertEqual(stored["field_values"], updated["field_values"])

    def test_excel_import_numeric_mapping_reports_invalid_rows_and_keeps_valid_rows(self):
        qa = self.session(self.create_user(RoleEnum.QA))
        precise = "0.123456789012345678900"
        for endpoint in (FEEDBACK, REFUND):
            with self.subTest(endpoint=endpoint):
                tracker = endpoint.rsplit("/", 1)[-1]
                quantity, = self.case_fields(tracker, "Quantity", field_type="integer")
                amount, = self.case_fields(tracker, "Amount", field_type="decimal")
                upload = excel_upload(
                    [quantity.label, amount.label],
                    [0, None],
                    [1, 12],
                    [None, "NaN"],
                    [-12, 1],
                    [None, 0.1],
                    [None, False],
                    [None, "=1+2"],
                    [None, "=1/4"],
                    [17, '=""'],
                    numeric_tokens={
                        "A3": "1.0000000000000001", "B5": precise, "B9": "0.25", "B10": None,
                    },
                )
                preview = self.assert_status(
                    self.client.post(f"{endpoint}/import/inspect", headers=qa, files=upload), 200
                ).json()
                mapping = {quantity.key: quantity.label, amount.key: amount.label}
                for key, label in mapping.items():
                    self.assertEqual(preview["suggestions"][key], label)
                preview_rows = {row["row_number"]: row["values"] for row in preview["preview"]}
                self.assertEqual(preview_rows[5][1], precise)
                self.assertEqual(preview_rows[8][1], "Formula has no saved result")
                self.assertEqual(preview_rows[10][1], "")
                with self.assertLogs("app", "INFO") as captured:
                    response = self.assert_status(self.client.post(
                        f"{endpoint}/import", headers=qa, files=upload,
                        data={"mapping": json.dumps(mapping)},
                    ), 200)
                summary, = [
                    record for record in captured.records if record.event == "tracker.import.committed"
                ]
                self.assertEqual(summary.reason_counts, {"invalid_numeric_value": 4})
                self.assertEqual(summary.request_id, response.headers["X-Request-ID"])
                self.assertNotIn(precise, JsonFormatter().format(summary))
                report = response.json()
                self.assertEqual(report["created_count"], 5)
                for row, expected in zip(
                    report["created"],
                    [("0", None), ("-12", precise), (None, "0.1"), (None, "0.25"), ("17", None)],
                    strict=True,
                ):
                    path = f"{endpoint}/{row['id']}"
                    self.addCleanup(self.client.delete, path, headers=qa)
                    values = self.assert_status(self.client.get(path, headers=qa), 200).json()["field_values"]
                    self.assertEqual((values.get(quantity.key), values.get(amount.key)), expected)
                self.assertEqual(report["skipped_count"], 4)
                for row, (number, field, code) in zip(
                    report["skipped"],
                    [
                        (3, quantity, "invalid_integer"),
                        (4, amount, "invalid_decimal"),
                        (7, amount, "invalid_decimal"),
                        (8, amount, "invalid_decimal"),
                    ],
                    strict=True,
                ):
                    self.assertEqual(row["row_number"], number)
                    self.assertEqual(row["field_key"], field.key)
                    self.assertEqual(row["code"], code)
                    self.assertIn(field.label, row["reason"])
                    self.assertNotIn("NaN", row["reason"])

                sparse_upload = excel_upload([], [0, 1.25])
                sparse = self.assert_status(
                    self.client.post(
                        f"{endpoint}/import/inspect", headers=qa, files=sparse_upload,
                    ),
                    200,
                ).json()
                self.assertEqual(sparse["headers"], ["Column 1", "Column 2"])
                self.assertEqual(sparse["preview"], [{"row_number": 2, "values": ["0", "1.25"]}])
                imported = self.assert_status(
                    self.client.post(
                        f"{endpoint}/import",
                        headers=qa,
                        files=sparse_upload,
                        data={"mapping": json.dumps({quantity.key: "Column 1", amount.key: "Column 2"})},
                    ),
                    200,
                ).json()
                self.assertEqual(imported["created_count"], 1)
                self.addCleanup(
                    self.client.delete, f"{endpoint}/{imported['created'][0]['id']}", headers=qa
                )

    def test_admin_numeric_field_assignments_control_import_and_my_reviews(self):
        admin = self.session(self.create_user(RoleEnum.ADMIN), htmx=True)
        qa = self.session(self.create_user(RoleEnum.QA))
        agent = self.create_user(RoleEnum.SUPPORT)
        label = f"Required quantity {uuid4().hex[:8]}"
        form = {"label": label, "field_type": "integer", "position": 1}
        for tracker, other in (("bad-feedback", "refund-check"), ("refund-check", "bad-feedback")):
            with self.subTest(tracker=tracker):
                admin_path = f"/admin/case-fields/{tracker}"
                api_path = f"/api/{tracker}"
                self.assert_status(
                    self.client.post(
                        f"{admin_path}/create", headers=admin,
                        data={**form, "required_for_import": "true", "show_in_my_reviews": "true"},
                    ),
                    200,
                )

                async def get_field():
                    async with async_session_factory() as db:
                        return (await list_fields(db, tracker))[1]

                field = self.client.portal.call(get_field)
                self.addCleanup(self.client.post, f"{admin_path}/{field.id}/archive", headers=admin)
                self.assertEqual(field.label, label)
                self.assertTrue(field.required_for_import)
                self.assertTrue(field.show_in_my_reviews)
                upload = excel_upload([label, "Source"], [0, "Workshop"], [None, "Missing quantity"])

                def inspect(tracker_name):
                    return self.assert_status(
                        self.client.post(f"/api/{tracker_name}/import/inspect", headers=qa, files=upload),
                        200,
                    ).json()

                self.assertIn(field.key, inspect(tracker)["required_fields"])
                self.assertNotIn(field.key, inspect(other)["field_labels"])
                report = self.assert_status(
                    self.client.post(
                        f"{api_path}/import", headers=qa, files=upload,
                        data={"mapping": json.dumps({field.key: label, "source": "Source"})},
                    ),
                    200,
                ).json()
                self.assertEqual(report["created_count"], 1)
                self.assertEqual(
                    report["skipped"], [{"row_number": 3, "reason": f"Missing required values: {label}"}],
                )
                record_path = f"{api_path}/{report['created'][0]['id']}"
                self.addCleanup(self.client.delete, record_path, headers=qa)
                assigned = self.assert_status(
                    self.client.patch(
                        record_path, headers=qa,
                        json={"agents": [{"user_id": agent.id, "kind": "Support"}]},
                    ),
                    200,
                ).json()
                self.assertEqual(assigned["field_values"][field.key], "0")

                def my_reviews():
                    return self.assert_status(
                        self.client.get(
                            "/partials/my-reviews", headers=self.session(agent), params={"cat": tracker}
                        ),
                        200,
                    ).text

                personal_view = my_reviews()
                self.assertIn(f"{label}:</span> 0", personal_view)
                self.assert_status(
                    self.client.post(f"{admin_path}/{field.id}/update", headers=admin, data=form), 200
                )
                self.assertNotIn(field.key, inspect(tracker)["required_fields"])
                self.assertNotIn(label, my_reviews())

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
        field, = self.case_fields("refund-check", "Refund paid on", field_type="date")
        long_feedback = "x" * 2_501
        columns = [field.label, "Date", "Support agents", "Customer feedback"]
        row = ["7.10.2026", "2026-10-05", agent.email, long_feedback]
        upload = excel_upload(columns, row)

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

    def test_excel_import_accepts_rows_without_agents_in_both_trackers(self):
        qa_user = self.create_user(RoleEnum.QA)
        qa = self.session(qa_user)
        other_qa = self.session(self.create_user(RoleEnum.QA))
        users_before = self.user_count()
        for endpoint, date_key in ((FEEDBACK, "fb_date"), (REFUND, "r_date")):
            tracker = endpoint.rsplit("/", 1)[1]
            custom, = self.case_fields(tracker, "Optional import note")
            for mapped in (True, False):
                with self.subTest(tracker=tracker, agent_columns_mapped=mapped):
                    marker = f"Agentless {uuid4().hex[:8]}"
                    assigned_qa_id = qa_user.id if mapped else None
                    upload = excel_upload(
                        ["Date", "Case", "Note", "Support", "Sales", "Ignored"],
                        ["2026-10-05", marker, "Saved note", None, "  ", None],
                        [None, " ", None, None, None, "unmapped data only"],
                    )
                    # Exercise the legacy date mapping on Bad Feedback too.
                    mapping = {
                        ("date" if tracker == "bad-feedback" else date_key): "Date",
                        "related_case": "Case",
                        custom.key: "Note",
                    }
                    data = {}
                    if mapped:
                        mapping.update(support_agents="Support", sales_agents="Sales")
                        data["assigned_qa_id"] = str(assigned_qa_id)
                    report = self.assert_status(self.client.post(
                        f"{endpoint}/import", headers=qa, files=upload,
                        data={**data, "mapping": json.dumps(mapping)},
                    ), 200).json()
                    self.assertEqual(report["created_count"], 1)
                    self.assertEqual(report["created_users"], [])
                    self.assertEqual(report["created"][0]["agent_labels"], [])
                    self.assertEqual(report["skipped"], [
                        {"row_number": 3, "reason": "no usable mapped values"},
                    ])
                    path = f"{endpoint}/{report['created'][0]['id']}"
                    self.addCleanup(self.client.delete, path, headers=qa)
                    saved = self.assert_status(self.client.get(path, headers=qa), 200).json()
                    self.assertEqual(saved["status"], "pending")
                    self.assertEqual(saved["assigned_qa_id"], assigned_qa_id)
                    self.assertEqual(saved["agents"], [])
                    self.assertEqual(saved["field_values"][date_key], "2026-10-05")
                    self.assertEqual(saved["related_case"], marker)
                    self.assertEqual(saved["field_values"][custom.key], "Saved note")
                    pending = self.assert_status(self.client.get(
                        f"/partials/{tracker}", headers=qa, params={"status": "pending"},
                    ), 200)
                    self.assertIn(marker, pending.text)
                    for headers, visible in ((qa, True), (other_qa, not mapped)):
                        queue = self.assert_status(self.client.get(
                            "/partials/to-review", headers=headers, params={"cat": tracker},
                        ), 200)
                        self.assertEqual(marker in queue.text, visible)
        self.assertEqual(self.user_count(), users_before)

    def test_excel_import_enforces_current_required_fields_only(self):
        qa = self.session(self.create_user(RoleEnum.QA))
        admin = self.session(self.create_user(RoleEnum.ADMIN), htmx=True)
        untouched = self.create_user(RoleEnum.QA)
        admin_path = "/admin/case-fields/bad-feedback"
        custom_label = f"Required note {uuid4().hex[:8]}"

        async def active_fields():
            async with async_session_factory() as db:
                return {f.label: f for f in await list_fields(db, "bad-feedback")}

        fields = self.client.portal.call(active_fields)
        date_field = next(f for f in fields.values() if f.key == "fb_date")
        self.assertFalse(date_field.required_for_import)
        date_update = f"{admin_path}/{date_field.id}/update"
        date_form = {"label": date_field.label, "field_type": "date"}

        def update_date(**options):
            self.assert_status(self.client.post(
                date_update, headers=admin, data={**date_form, **options},
            ), 200)

        manual = self.assert_status(self.client.post(
            FEEDBACK, headers=qa, json={"related_case": "Before import requirements"},
        ), 201).json()
        manual_path = f"{FEEDBACK}/{manual['id']}"
        self.addCleanup(self.client.delete, manual_path, headers=qa)
        self.addCleanup(update_date)
        self.assert_status(self.client.post(
            f"{admin_path}/create", headers=admin,
            data={"label": custom_label, "field_type": "text", "required_for_import": "true"},
        ), 200)
        custom = self.client.portal.call(active_fields)[custom_label]
        self.assertTrue(custom.required_for_import)

        def archive_custom():
            self.assert_status(self.client.post(
                f"{admin_path}/{custom.id}/archive", headers=admin,
            ), 200)

        self.addCleanup(archive_custom)
        upload = excel_upload(
            ["Date", "Note", "Support", "Sales", "Source"],
            ["2026-10-05", "valid", None, f"accepted-{uuid4().hex}", "Optional source"],
            ["  ", "  ", untouched.email, f"rejected-{uuid4().hex}", None],
            ["invalid date", "valid", None, f"bad-date-{uuid4().hex}", None],
            ["2026-10-05", "valid", None, f"too-long-{uuid4().hex}", "x" * 256],
            ["2026-10-05", "valid", None, f"excluded-{uuid4().hex}", None],
        )
        mapping = {
            "date": "Date", custom.key: "Note", "source": "Source",
            "support_agents": "Support", "sales_agents": "Sales",
        }

        def inspect(endpoint=FEEDBACK):
            return self.assert_status(self.client.post(
                f"{endpoint}/import/inspect", headers=qa, files=upload,
            ), 200).json()

        def commit(fields, status, excluded=()):
            report = self.assert_status(self.client.post(
                f"{FEEDBACK}/import", headers=qa, files=upload,
                data={"mapping": json.dumps(fields), "excluded_rows": json.dumps(excluded)},
            ), status).json()
            for row in report.get("created", []):
                self.addCleanup(self.client.delete, f"{FEEDBACK}/{row['id']}", headers=qa)
            return report

        self.assertEqual(inspect()["required_fields"], [custom.key])
        update_date(required_for_import="true")
        self.assertEqual(set(inspect()["required_fields"]), {"fb_date", custom.key})
        refund_preview = inspect(REFUND)
        self.assertEqual(refund_preview["required_fields"], [])
        self.assertNotIn(custom.key, refund_preview["field_labels"])
        self.assertIn(date_field.label, commit({**mapping, "date": "Missing header"}, 400)["detail"])
        self.assertIn(custom_label, commit({k: v for k, v in mapping.items() if k != custom.key}, 400)["detail"])

        users_before = self.user_count()
        report = commit(mapping, 200, excluded=[6])
        self.assertEqual(report["created_count"], 1)
        self.assertEqual(len(report["created"][0]["agent_labels"]), 1)
        self.assertEqual(len(report["created_users"]), 1)
        skipped = {row["row_number"]: row["reason"] for row in report["skipped"]}
        self.assertEqual(set(skipped), {3, 4, 5, 6})
        self.assertIn(date_field.label, skipped[3])
        self.assertIn(custom_label, skipped[3])
        self.assertIn("unparsable date", skipped[4])
        self.assertIn("maximum 255 characters", skipped[5])
        self.assertEqual(skipped[6], "excluded in preview")
        self.assertEqual(self.user_count(), users_before + 1)

        async def roles():
            async with async_session_factory() as db:
                return (await db.get(User, untouched.id)).roles

        self.assertEqual(self.client.portal.call(roles), [RoleEnum.QA])
        self.assertEqual(
            self.assert_status(self.client.get(manual_path, headers=qa), 200).json(), manual
        )
        self.assert_status(self.client.patch(
            manual_path, headers=qa, json={"related_case": "Manual edits remain optional"},
        ), 200)
        self.assert_status(self.client.post(f"{manual_path}/complete", headers=qa), 200)
        archive_custom()
        self.assertNotIn(custom.key, inspect()["field_labels"])
        self.assertEqual(inspect()["required_fields"], ["fb_date"])
        update_date()
        self.assertEqual(inspect()["required_fields"], [])
        self.assertEqual(commit({"source": "Source"}, 200)["created_count"], 1)

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
        supplier, resolution = self.case_fields("refund-check", "Supplier", "Resolution")
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
