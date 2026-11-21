"""Seed a QA account and print a signed cookie for the isolated Docker smoke test."""

import asyncio

from app.db.database import engine
from app.models import RoleEnum
from tests.fixtures import create_user, session_cookie


async def main() -> None:
    try:
        user = await create_user(RoleEnum.QA)
        print(session_cookie(user.id))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
