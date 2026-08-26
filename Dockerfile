# GotYourScore app image: Python deps via uv (locked), frontend assets
# (compiled CSS, vendored JS/fonts) are committed to the repo, so no Node
# stage is needed at build time.

FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# uv from the official distroless image pin; kept out of the runtime venv.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# Lockfile-first layer: dependency install only reruns when uv.lock changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

ENV PATH="/app/.venv/bin:$PATH"

EXPOSE 8000

# Apply migrations, then serve. DATABASE_URL must point at the `db`
# compose service (see docker-compose.yml).
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
