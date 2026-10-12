# Repository Guidelines

## Project Overview

AfterQuery technical-test scaffold: a FastAPI API with health/readiness probes, JWT code and adapters, plus a separate Vite/React counter starter. No integrated product flow or frontend API calls yet.

## Architecture & Data Flow

- `entrypoints/api/http/asgi.py` → `ASGIFactory` → `/api/v1` routers. The factory attaches DB/error-tracker adapters to FastAPI state, configures middleware, and connects/disconnects services during its async lifespan. Route dependencies obtain DB sessions from app state; readiness returns 503 if the DB ping fails.
- `src/app/ports.py` declares contracts implemented in `src/infra/`; use cases depend on domain schemas/ports. `src/shared/` supplies context and logging. `frontend/src/main.tsx` renders `App.tsx` with component-local state.

## Key Directories

- `src/app/`: use cases/ports; `src/domain/`: schemas/model base; `src/infra/`: adapters and Alembic migrations.
- `src/conf/`: settings; `src/shared/`: context/logging; `entrypoints/api/http/fastapi_/controllers/v1/`: API routers; `entrypoints/cli/`: Typer commands.
- `tests/`: backend tests; `frontend/src/`: React UI; `scripts/`: deployment startup.

## Development Commands

Run backend commands from the repository root and frontend commands from `frontend/`:

```sh
just deps                         # uv sync --frozen
uv build                          # Hatch-backed Python sdist and wheel
uv run python manage.py dev         # Typer → Uvicorn; requires configured DB
uv run python manage.py prod        # Typer → Gunicorn
uv run pytest                     # backend tests; requires a container runtime
just cov                          # coverage run -m pytest; coverage report
just pc                           # pre-commit: Ruff autofix/format, Pyright, Bandit, hygiene
cd frontend
bun install --frozen-lockfile
bun run dev                       # Vite development server
bun run build                     # tsc -b && vite build
bun run lint                      # Oxlint
```

Without PostgreSQL, set `CONNECT_TO_SERVICES_ON_STARTUP=false` for a `/health` smoke check (`/ready` remains unhealthy). `manage.py` is the dev/prod CLI entrypoint; `just dev`/`just prod` call it via `python3`, so ensure that resolves to the Python 3.14 project environment. Do **not** run `scripts/startup.sh` against a valuable `.env`: `scripts/extraction.py` overwrites it with an empty file.

## Code Conventions & Common Patterns

- Python: typed `snake_case` modules/functions and `PascalCase` `*Port`/`*Adapter` classes; absolute imports (`app`, `domain`, `infra`, `conf`, `shared`). Ruff uses 88 columns/import sorting; Pyright is strict.
- Async FastAPI routes use dependencies in `entrypoints/api/http/fastapi_/utils/state.py`; SQLAlchemy sessions roll back on failure. Settings instantiate at import time; app startup binds shared context.
- Preserve error contracts: JWT failures become `GenericException` (401); handled HTTP-client failures return `(response, error)`; LLM failures log and re-raise. Frontend uses functional TSX, single quotes/no semicolons, CSS imports, and local React state.

## Important Files

- `pyproject.toml`, `Justfile`, `.python-version`, `uv.lock`: Python dependencies, commands and tooling.
- `manage.py` → `entrypoints/cli/commands.py`: Typer dev/prod dispatch; `src/conf/_settings.py`, `entrypoints/api/http/fastapi_/factory.py`: configuration and app lifecycle; `alembic.ini`, `src/infra/database/migrations/env.py`: migrations (no revisions currently).
- `frontend/package.json`, `frontend/bun.lock`, `frontend/vite.config.ts`: scripts/build config; `tests/conftest.py`: shared DB/ASGI fixtures.

## Runtime/Tooling Preferences

- Python **3.14+** via `uv` (`.python-version` pins 3.14); system `python3` may differ. Use committed `uv.lock`/`bun.lock`, `just` for backend recipes, Bun for frontend scripts. Start backend commands at the repository root: settings read `pyproject.toml` and Git metadata relative to the working directory. No frontend test script or runtime-version pin is declared.

## Testing & QA

- After any edit, run `just pc` from the repository root; fix hook failures and rerun after auto-fixes. After major implementations, run `just cov` and require passing tests and coverage. Activate the `uv` environment first or prefix recipes with `uv run` (for example, `uv run just pc`).

- Pytest runs async tests with session-scoped loops. `tests/conftest.py` autostarts Testcontainers `postgres:18`, runs the ASGI lifespan and provides an HTTPX `ASGITransport` client; **even one selected test requires a container runtime**. DB tests flush/roll back per test; HTTP tests override mounted-app dependencies.
- Write behavior-focused `async def test_*` cases as in `tests/test_probes.py`/`tests/test_reverse_url.py`. `just cov` enforces **90%** coverage of `app`, `infra`, `domain`, `shared`, `conf` (not `entrypoints/`); plain `uv run pytest` does not enforce coverage.
