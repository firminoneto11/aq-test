# AI Interviewer frontend

React 19 / TypeScript / Vite candidate interface for the FastAPI AI Interviewer. The backend controls job packs, interview questions, accepted answers, reasons, durable transcripts, and evaluations. Browser audio goes through LiveKit; this app does not call speech or model providers directly.

## Run locally

From `frontend/`:

```sh
bun install --frozen-lockfile
cp .env.example .env.local
bun run dev --host localhost --port 5173 --strictPort
```

`VITE_API_BASE_URL` must be an absolute API URL **including `/api/v1`**, for example `http://localhost:8000/api/v1`. Vite embeds this value in the bundle at build time. The backend must be running with seeded jobs, a manually provisioned reviewer account, and `ALLOWED_ORIGINS` including the exact `http://localhost:5173` origin. `localhost` and `127.0.0.1` are different browser origins. A real voice interview also needs a working backend voice worker, LiveKit media, OpenRouter, Deepgram, and ElevenLabs credentials/credits configured **server-side**. Microphone access requires HTTPS or secure localhost.

```sh
bun run test
bun run lint
VITE_API_BASE_URL=http://localhost:8000/api/v1 bun run build
bun run preview --host localhost --port 5173 --strictPort
```

Use a current desktop Chrome or Edge browser with a window at least 1024px wide. No signup or password-reset endpoint exists; obtain the shared reviewer account from the backend operator. Sign in, select a server-provided job, then allow microphone access to start. The frontend requests a voice slot only after permission succeeds. If browser playback is blocked, click **Enable audio**. Live captions can change; saved Q/A and evidence come from the API. **Leave for now** disconnects locally but retains the interview for an explicit Resume. **End interview** cancels an unfinished attempt. Naturally completed interviews expose the stored evaluation and transcript even after voice disconnects.

The API JWT stays in memory; reloading requires signing in again. The interview URL and owner-keyed request UUIDs may remain in tab storage to recover unknown create/connect outcomes. No passwords, tokens, raw audio, captions, or transcript text are persisted by the frontend. A five-minute LiveKit grant is not the API credential. The app does not store raw audio/video; provider-side retention depends on provider accounts. People sharing the reviewer login share that account's saved interviews.

Only a local browser workflow is supplied. There is no mobile interview guarantee, frontend production hosting, replay, typed-answer flow, or interview-history listing. The [consumer contract](../zFiles/frontend-integration-guide.md) defines the backend API and recovery semantics.
