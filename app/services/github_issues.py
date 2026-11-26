"""Create website issues as a repository-scoped GitHub App installation."""

import asyncio
import base64
import time
from dataclasses import dataclass

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import RSAKey

from app.core.config import Settings

_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
    "User-Agent": "GotYourScore-Bug-Reports",
}


@dataclass(frozen=True, slots=True)
class GitHubIssue:
    number: int
    url: str


class GitHubIssueError(Exception):
    """Do not expose credentials, report text or provider response bodies."""

    def __init__(self, *, may_have_published: bool = False) -> None:
        super().__init__("GitHub issue creation failed")
        self.may_have_published = may_have_published


def _app_jwt(settings: Settings) -> str:
    private_key = settings.BUG_REPORT_GITHUB_PRIVATE_KEY_BASE64
    if private_key is None or settings.BUG_REPORT_GITHUB_APP_ID is None:
        raise GitHubIssueError()
    now = int(time.time())
    try:
        pem = base64.b64decode(private_key.get_secret_value(), validate=True)
        return jwt.encode(
            {"alg": "RS256"},
            {"iat": now - 60, "exp": now + 540, "iss": str(settings.BUG_REPORT_GITHUB_APP_ID)},
            RSAKey.import_key(pem),
        )
    except (ValueError, TypeError, JoseError):
        raise GitHubIssueError() from None


async def create_issue(settings: Settings, *, title: str, body: str) -> GitHubIssue:
    repository = settings.BUG_REPORT_GITHUB_REPOSITORY
    installation_id = settings.BUG_REPORT_GITHUB_INSTALLATION_ID
    if repository is None or installation_id is None:
        raise GitHubIssueError()
    app_jwt = _app_jwt(settings)
    async with httpx.AsyncClient(
        base_url="https://api.github.com", headers=_HEADERS,
        timeout=10, follow_redirects=False,
    ) as client:
        try:
            async with asyncio.timeout(12):
                response = await client.post(
                    f"/app/installations/{installation_id}/access_tokens",
                    headers={"Authorization": f"Bearer {app_jwt}"},
                    json={
                        "repositories": [repository.split("/", 1)[1]],
                        "permissions": {"issues": "write"},
                    },
                )
            response.raise_for_status()
            installation_token = response.json()["token"]
            if not isinstance(installation_token, str) or not installation_token.strip():
                raise GitHubIssueError()
        except (httpx.HTTPError, TimeoutError, ValueError, KeyError, TypeError):
            raise GitHubIssueError() from None

        # No retry: GitHub may have created the issue before a connection failed.
        try:
            async with asyncio.timeout(12):
                response = await client.post(
                    f"/repos/{repository}/issues",
                    headers={"Authorization": f"Bearer {installation_token}"},
                    json={"title": title, "body": body, "labels": ["bug"]},
                )
        except (httpx.HTTPError, TimeoutError):
            raise GitHubIssueError(may_have_published=True) from None
        if response.status_code != 201:
            raise GitHubIssueError(may_have_published=response.status_code >= 500)
        try:
            number = response.json()["number"]
        except (ValueError, KeyError, TypeError):
            raise GitHubIssueError(may_have_published=True) from None
        if type(number) is not int or number <= 0:
            raise GitHubIssueError(may_have_published=True)
        return GitHubIssue(number, f"https://github.com/{repository}/issues/{number}")
