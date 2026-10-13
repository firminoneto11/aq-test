# AI Interviewer platform

FastAPI API, a separate LiveKit Agents voice worker, and a Vite/React frontend. The backend provides three English job packs, password/JWT login, six-to-ten-question interviews with two mandatory answer-dependent follow-ups, persisted Q/A and evidence-backed evaluations. It exposes interview signals and deterministic question-selection reasons for the first two stretch goals.

## Requirements

- Python 3.14 and `uv` for local commands; a Docker-compatible runtime for the backend tests and Compose.
- PostgreSQL 18 (Compose provides it), a LiveKit Cloud media project, and API keys for OpenRouter, Deepgram and ElevenLabs to run voice interviews. The voice worker fails visibly if keys are missing; HTTP jobs, probes and saved-session reads do not require provider keys.
- Separate, strong JWT and database passwords. Do not copy example placeholders as credentials.

## Configuration

The root `.env` may contain unrelated values. **Do not replace it.** Start from `.env.example` in a new private `.env.interviewer` file (or merge only the required keys into an existing environment). Generate distinct random alphanumeric/hex secrets for `POSTGRES_PASSWORD` and `JWT_SECRET`; the JWT secret must contain at least 32 bytes. Set `DB_SERVER` to the Compose-hosted DB URL (`db:5432`) with the password URL-encoded. Configure `ALLOWED_ORIGINS` as a JSON array containing the exact frontend origin. Set the LiveKit URL/key/secret, `OPENROUTER_API_KEY`, `DEEPGRAM_API_KEY`, and `ELEVEN_API_KEY` server-side only. Never put those keys in a `VITE_*` variable.

Both live planning (`INTERVIEW_PLANNER_MODEL`) and asynchronous final evaluation (`INTERVIEW_LLM_MODEL`) default to `openai/gpt-6-luna` through OpenRouter; each remains independently overridable by environment. OpenRouter advertises response-format support, and synthetic strict-schema planning and evaluation requests were accepted on this installation within their configured deadlines. Provider latency still varies. Planning retains the 20-second `LLM_TIMEOUT_SECONDS` turn limit; asynchronous grading uses `EVALUATION_TIMEOUT_SECONDS` and does not block spoken questions. Changing models alone does not fix a one-question stall caused by pending voice input.

The voice worker uses Silero VAD for speech activity/interruptions and LiveKit's local audio `inference.TurnDetector(version="v1-mini")` to judge whether a candidate's spoken turn is complete. This model is built into the pinned Agents installation (`livekit-local-inference` is already locked and included in the image), so it needs no additional provider key or model-download command. Its local CPU inference requires capacity on the voice host (the SDK notes about 244 MB resident model preload); default endpointing still bounds pauses and cannot guarantee that every unfinished thought is retained.

Structured OpenRouter requests disable both LangChain's and the underlying SDK's automatic retries; the interview service owns its 20-second live-turn budget and at most one corrective response. A final evaluation with invalid evidence stays failed rather than fabricating a score; after fixing provider availability or output quality, the owner can explicitly retry with `POST /api/v1/interviews/{id}/evaluation` without replaying audio.

If a VAD-only interruption drops the normal SDK reply after an accepted answer, the voice worker waits for silence and rechecks that saved pending answer under its run/epoch fences. It then speaks only the server-approved next question, without requiring another finalized utterance or making a second interviewer in the browser.

Use `BACKEND_ENV_FILE=.env.interviewer docker compose --env-file .env.interviewer ...` for each Compose command, so both interpolation and container `env_file` read the intended private file instead of an unrelated `.env`. Leave `BACKEND_ENV_FILE` unset only when the root `.env` is deliberately configured for this application.

## Local backend

From the repository root, with the private environment file configured:

```sh
BACKEND_ENV_FILE=.env.interviewer docker compose --env-file .env.interviewer up -d --build
BACKEND_ENV_FILE=.env.interviewer docker compose --env-file .env.interviewer run --rm --no-deps api python manage.py create-user --email reviewer@example.com
```

`create-user` prompts twice without a password argument. There is no public signup or refresh endpoint. The Compose services are `db` (Postgres 18), `migrate` (one-shot Alembic migration and three-job seed), `api` (Gunicorn/FastAPI), and `agent` (one independent LiveKit worker). The API is bound to `127.0.0.1:8000` by default; `API_HOST_PORT` and `POSTGRES_HOST_PORT` can change the local host ports. Running only `db migrate api` is useful for API development without speech keys. The agent connects outward to LiveKit Cloud; the container does not need an inbound media port.

Useful probes: `GET /api/v1/probes/health` and `GET /api/v1/probes/ready`. The catalogue is `GET /api/v1/jobs`. The mounted API's development OpenAPI page is `/api/v1/_internal/docs` (disabled in production/staging). The backend contract includes:

| Purpose | Endpoint |
| --- | --- |
| Login / current account | `POST /api/v1/auth/token` (form-encoded `username` and `password`), `GET /api/v1/auth/me` |
| Jobs and sessions | `GET /api/v1/jobs`, `POST /api/v1/interviews`, `GET /api/v1/interviews/{id}` |
| Voice | `POST /api/v1/interviews/{id}/connect`, `POST /api/v1/interviews/{id}/end` |
| Results | `GET /api/v1/interviews/{id}/transcript`, `GET`/`POST /api/v1/interviews/{id}/evaluation` |

Protected HTTP requests use the application bearer JWT; the separate, short-lived LiveKit participant token returned by `/connect` is **only** for its specified room. A backend-created interview is resumable after disconnect; an unanswered question may replay under the same question ID. The server, not the browser, enforces question counts and determines natural completion. Interview text and evaluations persist in PostgreSQL, not raw audio/video. `record=False` in the worker does not disable project-level LiveKit recordings/observability: turn those off separately for the text-only data policy. Provider-side data settings require their own review.

For frontend DTOs, microphone/audio flow, reconnection, idempotency, and status handling, see [`zFiles/frontend-integration-guide.md`](zFiles/frontend-integration-guide.md). It records the original backend integration contract; the implemented frontend and API are authoritative for current behavior. The companion [`zFiles/interviewer-backend-plan.md`](zFiles/interviewer-backend-plan.md) records the historical backend-only plan.

## Fly.io deployment

Fly Machines host three separate apps: `aq-interviewer-fn11-api` (public HTTPS), `aq-interviewer-fn11-agent` (outbound-only voice worker), and `aq-interviewer-fn11-web` (public HTTPS static frontend). They share Managed Postgres cluster `aq-interviewer-fn11-db` in `iad`. The Fly API config runs Alembic migrations and idempotently seeds three jobs once as its release command; the worker must not own schema migrations. The attached `DB_SERVER` secret is a Fly Postgres URL normalized to asyncpg at runtime. Keep the API and worker always on so voice grants do not outlive a stopped worker.

Keep `JWT_SECRET`, `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `OPENROUTER_API_KEY`, `DEEPGRAM_API_KEY`, and `ELEVEN_API_KEY` as Fly secrets on **both** backend apps. Import them from secure stdin (`fly secrets import --stage -a <app>`), never from a tracked file, Docker build argument, or a command-line value. Attach the MPG cluster separately to each backend app with `fly mpg attach <cluster-id> -a <app> --variable-name DB_SERVER`. The web app has no provider secrets: `VITE_API_BASE_URL=https://aq-interviewer-fn11-api.fly.dev/api/v1` is public and baked into `frontend/Dockerfile`. API `ALLOWED_ORIGINS` allows only `https://aq-interviewer-fn11-web.fly.dev` in production.

From the repository root, after configuring the managed database and backend secrets:

```sh
fly deploy --config fly.api.toml --ha=false
fly deploy --config fly.agent.toml --ha=false
(cd frontend && fly deploy --config fly.toml --ha=false)
fly ssh console -a aq-interviewer-fn11-api --pty -C "python manage.py create-user --email reviewer@example.com"
```

The last command prompts privately for the reviewer password; run it once per email. Check `https://aq-interviewer-fn11-api.fly.dev/api/v1/probes/ready` and `https://aq-interviewer-fn11-api.fly.dev/api/v1/jobs` before opening `https://aq-interviewer-fn11-web.fly.dev`. An API health check does not prove the voice agent has registered with LiveKit; check `fly status -a aq-interviewer-fn11-agent` and `fly logs -a aq-interviewer-fn11-agent` when voice setup fails. The worker runs the bundled local `v1-mini` turn detector; size its Machine for local CPU inference. On future deploys, rebuild the frontend when its public API URL changes.

## Development and checks

```sh
uv sync --frozen
uv run python manage.py dev
uv run python -m livekit.agents download-files
uv run python -m entrypoints.voice.agent dev
uv run pytest
uv run just pc
uv run just cov
```

Python tests autostart a Postgres 18 Testcontainer; even one selected test needs a container runtime. `just cov` enforces the repository's existing 90% backend coverage threshold. Apply committed Alembic migrations before running the API/worker on a new database; production does not call `create_all`.
