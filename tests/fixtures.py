"""Fixtures that only write to the isolated CI database."""

import base64
import json
import unittest
from uuid import uuid4

from fastapi.testclient import TestClient
from itsdangerous import TimestampSigner
from sqlalchemy.engine import make_url

from app.core.config import get_settings
from app.core.security import SESSION_USER_ID_KEY
from app.db.database import async_session_factory
from app.models import RoleEnum, User


def require_test_database() -> None:
    if make_url(get_settings().DATABASE_URL).database != "gotyourscore_ci":
        raise RuntimeError("These tests require the dedicated gotyourscore_ci database")


async def create_user(*roles: RoleEnum) -> User:
    require_test_database()
    async with async_session_factory() as db:
        user = User(
            email=f"test-{uuid4().hex}@example.com",
            name="CI User",
            roles=list(roles),
        )
        db.add(user)
        await db.commit()
        return user


def session_cookie(user_id: int) -> str:
    require_test_database()
    payload = base64.b64encode(json.dumps({SESSION_USER_ID_KEY: user_id}).encode())
    return TimestampSigner(get_settings().SECRET_KEY).sign(payload).decode()


class PostgresTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        require_test_database()
        # Keep the app and fixture queries on the same event loop as the DB pool.
        from app.db.database import engine
        from app.main import app

        cls.client = cls.enterClassContext(TestClient(app, follow_redirects=False))
        cls.addClassCleanup(cls.client.portal.call, engine.dispose)

    def create_user(self, *roles):
        return self.client.portal.call(create_user, *roles)

    def session(self, user, *, htmx=False):
        headers = {"Cookie": f"session={session_cookie(user.id)}"}
        if htmx:
            headers["HX-Request"] = "true"
        return headers

    def assert_status(self, response, status):
        self.assertEqual(response.status_code, status, response.text[:1000])
        return response
