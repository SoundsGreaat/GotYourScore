# GotYourScore

Internal QA tool for scoring support-agent cases. FastAPI backend with async SQLAlchemy (asyncpg) + Alembic migrations, server-rendered Jinja2 templates enhanced with HTMX and daisyUI v5. Google-OAuth-only auth.

## Features

- **Case reviews** — QA staff score support cases against configurable scorecards (per case type, with multipliers); every saved review embeds a snapshot of the then-active scoring rules.
- **Delegation** — Supervisors/Admins create pending reviews and route them to a specific QA or to a shared queue every QA sees.
- **Bad Feedback & Refund Check** — independent, unscored case trackers with Support/Sales agent cards, a pending → complete lifecycle, monthly views, and Excel import (inspect → confirm mapping → commit).
- **Quotas & compliance** — each support agent has a monthly quota (default 6) per reporting period; dashboards track completion and average scores per agent.
- **AI assistance** — OpenRouter-backed QA-note refactoring, Bad Feedback comment rewriting, preview scoring and auto-scoring of pasted ticket transcripts (503 when no API key is configured), with Admin-managed versioned system prompts.
- **Admin panel** — HTMX panel for users (incl. soft-deleted), scorecards, system prompts, and configurable case fields for each tracker.

## Tech stack

| Layer     | Tech                                                            |
|-----------|-----------------------------------------------------------------|
| Backend   | Python ≥3.12, FastAPI, Starlette sessions, async SQLAlchemy 2   |
| Database  | PostgreSQL (asyncpg driver), Alembic migrations                 |
| Frontend  | Jinja2, HTMX 4, daisyUI v5 + Tailwind CSS v4 (compiled), Quill  |
| Tooling   | uv (Python deps), npm (CSS build + asset vendoring), Docker     |

## Environment setup

From the repository root, create `.env` if it does not already exist. This step applies to both quickstarts.

**Linux (Bash)**

```bash
cp .env.example .env
```

**Windows (PowerShell)**

```powershell
Copy-Item .env.example .env
```

Edit `.env` as described in [Configuration](#configuration).

## Quickstart (local)

For local development, install Python ≥3.12, [uv](https://docs.astral.sh/uv/), and Docker with Compose v2. Node.js ≥20 is required only for rebuilding frontend assets.

After [setting up `.env`](#environment-setup), run these commands from the repository root in Bash or PowerShell.

```sh
docker compose up -d --wait db
uv sync --locked --python 3.12
uv run --locked alembic upgrade head
uv run --locked uvicorn app.main:app --reload
```

The app serves on http://127.0.0.1:8000 (liveness probe: `GET /ping`).

## Quickstart (Docker)

With Docker Compose v2 installed and [`.env` set up](#environment-setup), run this command from the repository root in Bash or PowerShell to start both PostgreSQL and the app.

```sh
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
- **Bad Feedback and Refund Check**: separate case trackers with the same per-agent cards, queue, completion, and Excel import workflow. Neither is scored or quota-counted. Views group by calendar month of `completed_at`; pending records are visible in every month.
- **Case fields**: Admin → **Case Fields** manages each tracker's field list independently. Both start with Date, Source, Customer info, Customer feedback, and Related case. Date uses the key `fb_date` in Bad Feedback and `r_date` in Refund Check. Drag a handle or use the arrows to reorder fields; the order saves automatically. Changes apply to the editor, listings, and Excel mapping. Renaming or reordering keeps the same field key; archiving hides a field without deleting historical values, and restoring makes them visible again. Customer info and new fields are hidden from My Reviews unless explicitly enabled. Workflow data (assignment, status, and per-agent cards) is managed separately.

## Frontend assets

Styling is compiled by Tailwind v4 CLI + daisyUI v5 (no CDN dependencies anywhere — HTMX, Quill, DOMPurify, SortableJS and the Inter font are vendored into `app/static/` by `scripts/vendor.mjs` and committed).

Author styles in `app/static/css/src/`. `input.css` imports the theme and component stylesheets into the single generated `app/static/css/app.css` bundle.

Reuse existing components before building new ones. The [dropdown macro](app/templates/macros/dropdowns.html) shares keyboard and search behavior across views. Case Fields and Scorecards share [drag handles and keyboard move buttons](app/templates/macros/admin.html) and [SortableJS setup](app/static/js/admin.js), while each screen controls when changes save.

The npm commands are the same in Bash and PowerShell.

```sh
npm ci               # install from the lockfile
npm run css:build    # rebuild app/static/css/app.css after markup or style changes
npm run css:watch    # while iterating
npm run build        # vendor assets + rebuild CSS
```

Docker always runs `npm ci` and rebuilds the assets from `package-lock.json`. The checked-in copies are only a convenience for running the app without Docker; they cannot mask an npm update in a Docker build. Run `npm ci && npm run build` locally after pulling npm changes.

## Dependency updates and CI

[Dependabot](.github/dependabot.yml) opens weekly version-update PRs for `uv`, npm (app and browser tests), Docker, Docker Compose and GitHub Actions. Routine releases wait at least 7 days (21 for Python/npm major releases); cooldown does not delay security PRs. There is no auto-merge. Upgrade PostgreSQL major versions separately with a data migration plan.

[CI](.github/workflows/ci.yml) runs on PRs, pushes to `main`, merge queues and daily. The required check includes:

- Dependency review on PRs; `uv audit` and both npm lockfile audits. npm findings at moderate severity and above block CI. The existing low-severity Quill advisory is still printed.
- Integration tests with PostgreSQL across the Python versions in the CI matrix: the migration of an older database preserves sample data; tests cover OAuth callback handling, sessions and role checks, review scoring and quota, both case trackers, Excel uploads and the no-key AI response. No real Google or OpenRouter credentials are used.
- Production Docker build with regenerated assets, a clean-volume boot and restart, migration and `/ping` checks, and Chromium tests against the running container. Fixable high/critical OS findings block both the app and PostgreSQL images. The [temporary PostgreSQL libxml2 exception](.github/trivy-postgres-ignore.yml) is printed in CI, expires on December 12, 2026, and should be removed as soon as the official image includes the fix.

After a successful push to `main`, the exact tested Linux/amd64 image is available under **Actions → CI → Artifacts** as `gotyourscore-<commit SHA>` for 14 days. Unzip it and use `docker load -i gotyourscore.tar` to import it. CI does not deploy to production.

**One-time GitHub setup** (repository admin, after this configuration lands on `main`):

1. Under **Settings → Security → Advanced Security** (also shown as **Code security**), enable **Dependency graph**, **Dependabot alerts** and **Dependabot security updates**. Docker image pins receive version PRs; Dependabot does not generate Docker CVE-specific PRs.
2. After the first CI run, add a ruleset or branch protection for `main`: require PRs, at least one human approval (dismiss stale approvals), and the **Required CI** status check from GitHub Actions. Require the branch to be up to date before merging, or use a merge queue. Avoid routine bypasses.

To run against a fresh, separate Compose stack locally (Bash; use Compose 2.24.4+):

```bash
export APP_ENV_FILE=tests/ci.env COMPOSE_FILE=docker-compose.yml:tests/compose.yml
export COMPOSE_PROJECT_NAME=gys-ci-local
docker compose up --build --wait
docker compose run --rm --no-deps -T -v "$PWD/tests:/app/tests:ro" app \
  python -m unittest discover -s tests -v
npm ci --prefix tests/browser --ignore-scripts
npm run --prefix tests/browser install:chromium   # add `-- --with-deps` on Linux if needed
export APP_BASE_URL="http://$(docker compose port app 8000)"
CI_SESSION_COOKIE="$(docker compose run --rm --no-deps -T \
  -v "$PWD/tests:/app/tests:ro" app python -m tests.browser_session)" \
  npm test --prefix tests/browser
docker compose down --volumes
```

The test overlay binds to random loopback ports and uses the `gotyourscore_ci` database. Use `down --volumes` only with the test project and overlay active. On Windows, the `COMPOSE_FILE` separator is `;` and environment variables are set with `$env:...`.

## Scripts

- `scripts/import_scorecard_csv.py` — import a scorecard from CSV. Writes to whatever `DATABASE_URL` points at; pass `--dry-run` to preview.
- `scripts/vendor.mjs` — vendors frontend dependencies (HTMX, Quill, DOMPurify, SortableJS, Inter font) into `app/static/`; wired into `npm run build`.
- `alembic/` — migrations (`uv run alembic upgrade head`). New models must be exported in `app/models/__init__.py` or autogenerate produces empty migrations.

## Project structure

```
app/
├── api/            # Routers: auth (Google OAuth), api/* JSON, pages (HTML/partials), admin
├── core/           # Settings (pydantic-settings), security helpers, text utils
├── db/             # Async engine/session, declarative base, model imports
├── models/         # SQLAlchemy models (users, reviews, scorecards, case trackers, settings)
├── schemas/        # Pydantic request/response schemas
├── services/       # Business logic: quotas, scoring, AI, users, case fields and Excel import
├── static/         # Compiled CSS, JS (app + vendored vendors), fonts
└── templates/      # Jinja2: base/dashboard/admin shells, partials, macros
alembic/            # Migration environment + versions
scripts/            # CSV import, asset vendoring
```

## API surface

- `/auth/*` — Google OAuth login/logout
- `/api/reviews/*` — create, AI auto-score, score preview, delegate (`/pending`, `/pending/bulk`, `/pending/mine-count`), complete/edit (PATCH), soft delete, quota, compliance
- `/api/bad-feedback/*` and `/api/refund-check/*` — independent case CRUD, completion, and Excel import (`/import/inspect`, `/import`); configurable values use the `field_values` object keyed by stable field keys
- `/api/assignments/*` — Supervisor/Admin QA staffing
- `/api/ai/*` — refactor QA notes / Bad Feedback comments, score notes into a scorecard, draft notes from ticked deductions (OpenRouter)
- `/api/system-prompts/*` — Admin-only LLM prompt management
- `/partials/*`, `/admin/*` — RBAC-guarded HTML fragments consumed by HTMX
- `/ping` — liveness probe
