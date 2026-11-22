"""Seed a QA/admin session and a past review for the isolated Docker smoke tests."""

import asyncio
from datetime import datetime, timedelta, timezone

from app.db.database import async_session_factory, engine
from app.models import CaseTypeEnum, Review, RoleEnum
from app.services.reporting_period import reporting_period_for
from tests.fixtures import create_user, session_cookie


async def main() -> None:
    try:
        user = await create_user(RoleEnum.QA, RoleEnum.ADMIN)
        agent = await create_user(RoleEnum.SUPPORT)
        # The period picker only offers past periods if reviews exist in them.
        current_start = reporting_period_for(datetime.now(timezone.utc))[0]
        async with async_session_factory() as db:
            db.add(
                Review(
                    support_agent_id=agent.id,
                    qa_id=user.id,
                    created_by=user.id,
                    case_type=CaseTypeEnum.NO_CASES,
                    created_at=current_start - timedelta(days=1),
                )
            )
            await db.commit()
        print(session_cookie(user.id))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
