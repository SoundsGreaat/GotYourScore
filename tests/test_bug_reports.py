"""Website bug report publication and GitHub App authentication contracts."""

import asyncio
import base64
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from joserfc import jwt
from joserfc.jwk import RSAKey

from app.api.endpoints import bug_reports
from app.core.ai_rate_limit import AiRateLimiter
from app.core.config import Settings, get_settings
from app.core.observability import JsonFormatter
from app.core.security import get_current_user
from app.db.database import get_db
from app.main import app
from app.services import bug_report_service, github_issues

_REPORT = {
    "title": "Case editor does not save",
    "steps": "Open the editor and click Save.",
    "actual": "A server error appears.",
    "expected": "The edited record should save.",
}
_BODY = "## Problem\n\nAn error appears.\n\nSubmitted via the GotYourScore website."
_HEADERS = {"X-Bug-Report": "1"}
_URL = "/api/bug-reports"


def settings(**overrides):
    return Settings(
        _env_file=None,
        **{
            "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost/gotyourscore_ci",
            "SECRET_KEY": "test-only-long-session-secret-for-unit-tests",
            "OPENROUTER_API_KEY": "unused-fake-key",
            "BUG_REPORT_GITHUB_REPOSITORY": "Example/Project",
            "BUG_REPORT_GITHUB_APP_ID": 456,
            "BUG_REPORT_GITHUB_INSTALLATION_ID": 123,
            "BUG_REPORT_GITHUB_PRIVATE_KEY_BASE64": "unused-fake-key",
            **overrides,
        },
    )


class BugReportApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.user_id = 1
        self.settings = settings()
        self.db = AsyncMock()
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ))
        self.enterContext(patch.dict(app.dependency_overrides, {
            get_db: lambda: self.db,
            get_current_user: lambda: SimpleNamespace(id=self.user_id),
            get_settings: lambda: self.settings,
        }))
        self.enterContext(patch.object(bug_reports, "_previews", {}))
        self.enterContext(patch.object(
            bug_reports, "_publication_limiter",
            AiRateLimiter(max_requests=3, window_seconds=3600, max_concurrent_requests=1),
        ))
        self.enterContext(patch.object(
            bug_reports, "reserve_ai_request", new_callable=AsyncMock,
            return_value=SimpleNamespace(release=AsyncMock()),
        ))
        self.prepare = self.enterContext(patch.object(
            bug_report_service, "prepare_report", new_callable=AsyncMock,
            return_value=SimpleNamespace(title="Redacted title", body=_BODY),
        ))
        self.create = self.enterContext(patch.object(
            github_issues, "create_issue", new_callable=AsyncMock,
            return_value=github_issues.GitHubIssue(12, "https://github.com/Example/Project/issues/12"),
        ))

    async def preview(self, **overrides):
        response = await self.client.post(
            f"{_URL}/preview", **({"headers": _HEADERS, "json": _REPORT} | overrides),
        )
        return response

    async def token(self):
        response = await self.preview()
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()["token"]

    async def publish(self, token):
        return await self.client.post(_URL, headers=_HEADERS, json={"token": token})

    async def test_only_the_current_checked_report_can_be_published(self):
        self.assertEqual((await self.publish("x" * 43)).json(), {"code": "expired_preview"})
        old_token = await self.token()
        preview = (await self.preview()).json()
        self.assertEqual((await self.publish(old_token)).status_code, 409)
        self.user_id = 2
        self.assertEqual((await self.publish(preview["token"])).status_code, 409)
        self.create.assert_not_awaited()
        self.user_id = 1
        published = await self.publish(preview["token"])
        self.assertEqual(published.status_code, 201, published.text)
        self.assertEqual(published.json()["number"], 12)
        self.assertEqual((await self.publish(preview["token"])).json(), published.json())
        self.create.assert_awaited_once_with(
            self.settings, title=preview["title"], body=preview["body"],
        )
        self.assertGreater(self.db.commit.await_count, 0)

    async def test_authentication_transaction_is_released_before_reading_the_body(self):
        async def body():
            self.db.commit.assert_awaited_once()
            yield json.dumps(_REPORT).encode()

        response = await self.client.post(
            f"{_URL}/preview",
            headers={**_HEADERS, "Content-Type": "application/json"},
            content=body(),
        )
        self.assertEqual(response.status_code, 200)

    async def test_concurrent_confirmation_cannot_post_twice(self):
        token = await self.token()
        started, finish = asyncio.Event(), asyncio.Event()

        async def create_issue(*args, **kwargs):
            started.set()
            await finish.wait()
            return self.create.return_value

        self.create.side_effect = create_issue
        task = asyncio.create_task(self.publish(token))
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            self.assertEqual((await self.publish(token)).status_code, 409)
        finally:
            finish.set()
            self.assertEqual((await task).status_code, 201)
        self.create.assert_awaited_once()

    async def test_failed_privacy_check_cannot_preview_or_publish(self):
        self.prepare.side_effect = bug_report_service.BugReportPreparationError("SECRET VALUE")
        response = await self.preview()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"code": "privacy_unavailable"})
        self.assertFalse(bug_reports._previews)
        self.create.assert_not_awaited()

    async def test_unclear_fields_are_returned_without_creating_a_preview(self):
        self.prepare.side_effect = bug_report_service.UnsuitableBugReportError(("steps",))
        response = await self.preview()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json(), {"code": "needs_details", "fields": ["steps"]})
        self.assertFalse(bug_reports._previews)

    async def test_auth_csrf_uploads_and_disabled_configuration(self):
        self.assertEqual((await self.preview(headers={})).status_code, 403)
        self.assertEqual(
            (await self.preview(headers={**_HEADERS, "Sec-Fetch-Site": "same-site"})).status_code,
            403,
        )
        attachment = await self.client.post(
            f"{_URL}/preview", headers=_HEADERS, files={"attachment": ("shot.png", b"PNG")},
        )
        self.assertEqual(attachment.status_code, 415)
        extra = await self.preview(json={**_REPORT, "PRIVATE INPUT": "image contents"})
        self.assertEqual(extra.json(), {"code": "invalid_report"})
        blank = await self.preview(json={**_REPORT, "steps": "   "})
        self.assertEqual(blank.json(), {"code": "invalid_report", "fields": ["steps"]})
        self.settings = settings(BUG_REPORT_GITHUB_APP_ID=None)
        self.assertEqual((await self.preview()).status_code, 404)
        app.dependency_overrides.pop(get_current_user)
        self.assertEqual((await self.preview()).status_code, 401)
        self.prepare.assert_not_awaited()

    async def test_expired_preview_and_ambiguous_github_failure_are_not_retried(self):
        with patch.object(bug_reports, "_PREVIEW_TTL_SECONDS", 0):
            token = await self.token()
            await asyncio.sleep(0.01)
        self.assertEqual((await self.publish(token)).status_code, 409)
        self.create.assert_not_awaited()
        token = await self.token()
        self.create.side_effect = github_issues.GitHubIssueError(
            may_have_published=True, reason_code="github_issue_status_failed", provider_status=503,
        )
        with self.assertLogs("app", "INFO") as captured:
            failure = await self.publish(token)
        self.assertEqual((failure.status_code, failure.json()), (502, {"code": "publication_uncertain"}))
        event, = [record for record in captured.records if record.event == "bug_report.publish"]
        self.assertEqual(
            (event.outcome, event.reason_code, event.provider_status),
            ("uncertain", "github_issue_status_failed", 503),
        )
        self.assertEqual(event.request_id, failure.headers["X-Request-ID"])
        formatted = JsonFormatter().format(event)
        self.assertNotIn(token, formatted)
        self.assertNotIn(_REPORT["title"], formatted)
        self.assertEqual((await self.publish(token)).status_code, 409)
        self.create.assert_awaited_once()


class GitHubIssueTests(unittest.IsolatedAsyncioTestCase):
    async def test_scoped_installation_token_and_bot_publication(self):
        key = RSAKey.generate_key()
        configured = settings(
            BUG_REPORT_GITHUB_PRIVATE_KEY_BASE64=base64.b64encode(key.as_pem(private=True)).decode(),
        )
        requests = []

        def github(request):
            requests.append(request)
            self.assertEqual(request.url.host, "api.github.com")
            self.assertEqual(request.method, "POST")
            self.assertEqual(request.headers["x-github-api-version"], "2022-11-28")
            if request.url.path == "/app/installations/123/access_tokens":
                self.assertEqual(json.loads(request.content), {
                    "repositories": ["Project"], "permissions": {"issues": "write"},
                })
                signed = jwt.decode(request.headers["authorization"].removeprefix("Bearer "), key)
                self.assertEqual(signed.header["alg"], "RS256")
                self.assertEqual(signed.claims["iss"], "456")
                self.assertEqual(signed.claims["exp"] - signed.claims["iat"], 600)
                return httpx.Response(201, json={"token": "installation-token"})
            self.assertEqual(request.url.path, "/repos/Example/Project/issues")
            self.assertEqual(request.headers["authorization"], "Bearer installation-token")
            self.assertEqual(json.loads(request.content), {
                "title": "Bug title", "body": _BODY, "labels": ["bug"],
            })
            return httpx.Response(201, json={"number": 123, "html_url": "https://wrong.invalid"})

        def client(**kwargs):
            return client_type(transport=httpx.MockTransport(github), **kwargs)

        client_type = httpx.AsyncClient
        with patch.object(httpx, "AsyncClient", side_effect=client):
            issue = await github_issues.create_issue(configured, title="Bug title", body=_BODY)
        self.assertEqual(len(requests), 2)
        self.assertEqual(issue, github_issues.GitHubIssue(
            123, "https://github.com/Example/Project/issues/123",
        ))

    async def test_invalid_private_key_does_not_reach_the_network(self):
        with (
            patch.object(httpx, "AsyncClient") as client,
            self.assertRaises(github_issues.GitHubIssueError),
        ):
            await github_issues.create_issue(settings(), title="Bug title", body=_BODY)
        client.assert_not_called()
