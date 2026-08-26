# GotYourScore

Internal QA tool for scoring support-agent cases. FastAPI backend with async SQLAlchemy (asyncpg) + Alembic migrations, server-rendered Jinja2 templates enhanced with HTMX and daisyUI v5. Google-OAuth-only auth.

## Features

- **Case reviews** — QA staff score support cases against configurable scorecards (per case type, with multipliers); every saved review embeds a snapshot of the then-active scoring rules.
- **Delegation** — Supervisors/Admins create pending reviews and route them to a specific QA or to a shared queue every QA sees.
- **Quotas & compliance** — each support agent has a monthly quota (default 6) per reporting period; dashboards track completion and average scores per agent.
- **AI assistance** — OpenRouter-backed note refactoring and preview scoring (503 when no API key is configured), with Admin-managed versioned system prompts.
- **Admin panel** — HTMX panel for users (incl. soft-deleted), scorecards and system prompts.

## Tech stack

| Layer     | Tech                                                            |
|-----------|-----------------------------------------------------------------|
| Backend   | Python ≥3.12, FastAPI, Starlette sessions, async SQLAlchemy 2   |
| Database  | PostgreSQL (asyncpg driver), Alembic migrations                 |
| Frontend  | Jinja2, HTMX 2, daisyUI v5 + Tailwind CSS v4 (compiled), Quill  |
| Tooling   | uv (Python deps), npm (CSS build + asset vendoring), Docker     |

## Quickstart (local)

Prerequisites: Python ≥3.12 with [uv](https://docs.astral.sh/uv/), Docker, Node.js ≥20 (only for rebuilding frontend assets).

```powershell
# 1. Start Postgres
docker compose up -d

# 2. Configure environment
Copy-Item .env.example .env   # then edit — see Configuration below

# 3. Install deps, migrate, run
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

The app serves on http://127.0.0.1:8000 (liveness probe: `GET /ping`).

## Quickstart (Docker)

Runs both services — Postgres and the app:

```powershell
Copy-Item .env.example .env   # then edit
docker compose up -d --build
```

The app container waits for the DB healthcheck, applies migrations (`alembic upgrade head`) and starts uvicorn on port 8000.

## Configuration

All settings live in `.env` (template: `.env.example`). Keys are **case-sensitive**; empty values coerce to `None` for optional keys. The app crashes without `DATABASE_URL`.

| Variable                | Required | Default                            | Description                                                        |
|-------------------------|----------|------------------------------------|--------------------------------------------------------------------|
| `DATABASE_URL`          | yes      | —                                  | Async DSN, e.g. `postgresql+asyncpg://postgres:postgres@localhost:5432/gotyourscore` |
| `SECRET_KEY`            | no       | insecure dev value                 | Signs session cookies — set a strong random string in production   |
| `GOOGLE_CLIENT_ID`      | no       | `None`                             | Google OAuth client id (login requires it)                         |
| `GOOGLE_CLIENT_SECRET`  | no       | `None`                             | Google OAuth client secret                                         |
| `ALLOWED_DOMAIN`        | no       | `example.com`                      | Only e-mails on this domain may log in; first login auto-registers the user with the Support role |
| `OPENROUTER_API_KEY`    | no       | `None`                             | Unset → AI endpoints return 503                                    |
| `SESSION_COOKIE_SECURE` | no       | `false`                            | Set `true` behind HTTPS                                            |
| `SESSION_MAX_AGE`       | no       | `None` (browser session)           | Cookie lifetime in seconds                                         |
| `MONTHLY_QUOTA`         | no       | `6`                                | Reviews per support agent per reporting period                     |

> Override `ALLOWED_DOMAIN` in your local `.env`, otherwise login is rejected unless your Google account is on `example.com`.

## Domain model notes

- **Multi-role users**: many-to-many `user_roles`; check via `User.has_role()` / `RoleChecker` / `is_reviewer()`. Roles: `Admin`, `Supervisor`, `QA`, `Support`.
- **Soft delete** on `User` and `Review` (`deleted_at`): soft-deleted users can't log in and drop out of quota/compliance math; soft-deleted reviews 404 from the API and stop counting toward quotas immediately.
- **Reporting periods**: the 26th of one month through the 25th of the next (UTC), named after its *closing* month (`app/services/reporting_period.py`). Review creation returns **409 once the quota is reached**; "No Cases" reviews have null score/scorecard but still count toward the quota.
- **Pending reviews** are quota-neutral; completion = PATCH with `raw_scorecard` (deliberately skips the quota gate). The last editor of a pending review becomes its executor; completed rows reject reassignment.
- **Scorecards**: exactly one active per case type; saved reviews embed `rules_snapshot` and score against it, never live rules. `error_name` on saved items is immutable.

## Frontend assets

Styling is compiled by Tailwind v4 CLI + daisyUI v5 (no CDN dependencies anywhere — HTMX, Quill, DOMPurify, SortableJS and the Inter font are vendored into `app/static/` by `scripts/vendor.mjs` and committed).

```powershell
npm install          # once
npm run css:build    # rebuild app/static/css/app.css after markup changes
npm run css:watch    # while iterating
npm run build        # vendor assets + rebuild CSS
```

## Scripts

- `scripts/import_scorecard_csv.py` — import a scorecard from CSV. Writes to whatever `DATABASE_URL` points at; pass `--dry-run` to preview.
- `alembic/` — migrations (`uv run alembic upgrade head`). New models must be exported in `app/models/__init__.py` or autogenerate produces empty migrations.

## Project structure

```
app/
├── api/            # Routers: auth (Google OAuth), api/* JSON, pages (HTML/partials), admin
├── core/           # Settings (pydantic-settings), security helpers, text utils
├── db/             # Async engine/session, declarative base, model imports
├── models/         # SQLAlchemy models (User, Review, Scorecard, SystemPrompt, Assignment)
├── schemas/        # Pydantic request/response schemas
├── services/       # Business logic: quotas, reporting periods, scorecards, AI, prompts
├── static/         # Compiled CSS, JS (app + vendored vendors), fonts
└── templates/      # Jinja2: base/dashboard/admin shells, partials, macros
alembic/            # Migration environment + versions
scripts/            # CSV import, asset vendoring
```

## API surface

- `/auth/*` — Google OAuth login/logout
- `/api/reviews/*` — create, delegate (`/pending`, `/pending/bulk`), complete/edit (PATCH), soft delete, quota, compliance
- `/api/assignments/*` — Supervisor/Admin QA staffing
- `/api/ai/*` — AI note refactoring & preview scoring (OpenRouter)
- `/api/system-prompts/*` — Admin-only LLM prompt management
- `/partials/*`, `/admin/*` — RBAC-guarded HTML fragments consumed by HTMX
- `/ping` — liveness probe
