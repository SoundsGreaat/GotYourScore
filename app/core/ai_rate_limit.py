"""In-process per-user guard for expensive AI requests.

The application currently runs as one Uvicorn process.  The limiter keeps
both a rolling request budget and a small per-user concurrency cap, preventing
one authenticated account from monopolising the configured OpenRouter budget.
"""

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from fastapi import HTTPException, status

from app.core.config import get_settings


class AiRateLimitExceeded(Exception):
    """Raised when an AI request exceeds the caller's configured budget."""

    def __init__(self, retry_after: int, detail: str) -> None:
        self.retry_after = retry_after
        self.detail = detail
        super().__init__(detail)


@dataclass
class AiRequestLease:
    """One admitted request; it must be released after the LLM call ends."""

    _limiter: "AiRateLimiter"
    user_id: int
    _released: bool = False

    async def release(self) -> None:
        """Release the per-user concurrency slot exactly once."""
        if self._released:
            return
        self._released = True
        await self._limiter.release(self.user_id)


class AiRateLimiter:
    """Sliding-window request limiter with a per-user concurrency cap.

    It intentionally counts a request at admission, including upstream errors:
    otherwise a failing provider could be retried without consuming budget.
    """

    def __init__(
        self,
        *,
        max_requests: int,
        window_seconds: int,
        max_concurrent_requests: int,
    ) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self.max_concurrent_requests = max_concurrent_requests
        self._request_times: dict[int, deque[float]] = defaultdict(deque)
        self._in_flight: dict[int, int] = defaultdict(int)
        self._lock = asyncio.Lock()

    async def acquire(self, user_id: int) -> AiRequestLease:
        """Reserve a request budget and concurrency slot for ``user_id``."""
        now = time.monotonic()
        async with self._lock:
            timestamps = self._request_times[user_id]
            cutoff = now - self.window_seconds
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()

            if self._in_flight[user_id] >= self.max_concurrent_requests:
                raise AiRateLimitExceeded(
                    retry_after=1,
                    detail="Too many AI requests are already in progress. Please wait.",
                )

            if len(timestamps) >= self.max_requests:
                retry_after = max(1, int(timestamps[0] + self.window_seconds - now) + 1)
                raise AiRateLimitExceeded(
                    retry_after=retry_after,
                    detail="AI request limit reached. Please try again shortly.",
                )

            timestamps.append(now)
            self._in_flight[user_id] += 1
            return AiRequestLease(self, user_id)

    async def release(self, user_id: int) -> None:
        """Return a concurrency slot after a buffered or streaming request."""
        async with self._lock:
            self._in_flight[user_id] -= 1
            if self._in_flight[user_id] <= 0:
                self._in_flight.pop(user_id, None)


_settings = get_settings()
ai_rate_limiter = AiRateLimiter(
    max_requests=_settings.AI_RATE_LIMIT_REQUESTS,
    window_seconds=_settings.AI_RATE_LIMIT_WINDOW_SECONDS,
    max_concurrent_requests=_settings.AI_MAX_CONCURRENT_REQUESTS_PER_USER,
)


async def reserve_ai_request(user_id: int) -> AiRequestLease:
    """Reserve a caller's AI budget or return a consistent HTTP 429."""
    try:
        return await ai_rate_limiter.acquire(user_id)
    except AiRateLimitExceeded as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=exc.detail,
            headers={"Retry-After": str(exc.retry_after)},
        ) from exc
