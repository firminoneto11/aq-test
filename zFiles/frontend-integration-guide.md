# Frontend integration guide — AI Interviewer

## Contract status and ownership

This guide records the integration contract defined for the backend phase. The service and frontend are now implemented and deployed; verify illustrative request/response shapes against the current code when integrating another client. The companion [backend execution plan](interviewer-backend-plan.md) records the backend decisions at that time; the root README lists current setup and hosted URLs.

The implemented frontend uses React/TypeScript/Vite in `frontend/`. This guide explains how clients consume the API, voice worker, database-backed session state, and evaluations; it is not a claim that every illustrative shape below is captured production output.

The backend owns job packs, question selection, counts, answer acceptance, follow-ups, rubric evidence, session ownership, transcript persistence, and evaluation. The frontend owns login UX, job selection, microphone permission, one audio connection, presentation, and recovery controls. Never implement a second interviewer in the browser, call AI vendors directly, synthesize the interviewer's speech locally, or decide when six-to-ten questions are complete.

## 1. Configuration and credentials

Use one public frontend setting:

```dotenv
VITE_API_BASE_URL=http://localhost:8000/api/v1
```

The value includes `/api/v1`; append route paths such as `/jobs`, not another `/api/v1`. For a deployed frontend, configure the reachable HTTPS API URL. A localhost URL in a deployed bundle refers to the reviewer's machine, not your server. Vite replaces `VITE_*` values at build time; changing server environment variables after building static assets does not rewrite the bundle.

The backend must allow the exact browser origin using `ALLOWED_ORIGINS`; the local recommendation is `http://localhost:5173`. `localhost` and `127.0.0.1` are different origins. Browser microphone access requires HTTPS or a secure localhost context. Browser requests must target the API's published port/URL, not Docker service names such as `api` or `db`.

Never put `JWT_SECRET`, LiveKit API secrets, `OPENROUTER_API_KEY`, `DEEPGRAM_API_KEY`, or `ELEVEN_API_KEY` in frontend environment variables, source, URLs, localStorage, analytics, or logs. The LiveKit server URL is returned in the connection grant; the browser does not mint room tokens or choose a room name.

Two different bearer credentials are used:

| Credential | Destination | Lifetime and handling |
| --- | --- | --- |
| Application access JWT | HTTP `Authorization: Bearer …` for protected `/api/v1` routes | 86400 seconds; keep in memory, re-login on expiry. No refresh endpoint exists. |
| LiveKit participant token | `room.connect(server_url, participant_token)` only | Up to 300 seconds from original grant issuance, scoped to one room/run. Keep in memory and do not reuse for another interview. |

The frontend default is an in-memory API JWT, not persistent token storage. A reload can require login again, followed by resuming the interview identified in the URL/tab state. It is safe to retain non-secret interview IDs and request UUIDs in tab-scoped storage, keyed by the authenticated user. Do not persist passwords, raw audio, or transcript caches there. JavaScript-readable storage is not an HttpOnly session mechanism.

The reviewer account is provisioned manually by the backend operator. There is no signup, account-management, password-reset, or refresh-token flow. People sharing that account share its data; do not describe it as private per-candidate accounts.

## 2. Login and HTTP conventions

`POST /auth/token` is form-encoded, not JSON. `username` contains the email address:

```http
POST /api/v1/auth/token
Content-Type: application/x-www-form-urlencoded

username=reviewer%40example.com&password=<URL-encoded password>
```

```json
{
  "access_token": "<application JWT>",
  "token_type": "bearer",
  "expires_in": 86400
}
```

Use `URLSearchParams` rather than constructing the form manually:

```ts
async function login(apiBase: string, email: string, password: string) {
  const response = await fetch(`${apiBase}/auth/token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ username: email, password }),
  })
  const body = await response.json()
  if (!response.ok) {
    throw new Error(typeof body.detail === 'string' ? body.detail : 'Login failed')
  }
  if (typeof body.access_token !== 'string' || body.token_type !== 'bearer') {
    throw new Error('Unexpected authentication response')
  }
  return body as LoginResponse
}
```

After login, call `GET /auth/me` with the API JWT to obtain `{id,email}` and establish the owner of any saved tab state. Protected JSON POST requests use `Content-Type: application/json` and the bearer header. No cookies or `credentials: 'include'` are required by this contract. The login page must not expose whether a particular email exists.

On HTTP 401, clear the API credential and private rendered data, stop local microphone capture/room connection, preserve only a safe return location/interview ID, and ask the user to log in. Do not loop refresh attempts against a nonexistent endpoint. A UI timer may warn about expiry; the backend's authorization response is authoritative.

Handled application errors have `{detail: string}`. FastAPI validation errors can instead have `{detail: [{loc, msg, type, ...}]}`. Handle both, and also handle a network error or non-JSON proxy response. Never log passwords, Authorization headers, room grants, or transcript payloads when reporting errors.

## 3. Endpoint reference

Paths below are relative to `VITE_API_BASE_URL`. Only login, probes, and the job catalogue are public.

| Method/path | Request | Success and important behavior |
| --- | --- | --- |
| `POST /auth/token` | Form `username`, `password` | Login response above; invalid credentials → 401 |
| `GET /auth/me` | Bearer JWT | `{id,email}` for an active user |
| `GET /jobs` | None | `{jobs:[{id,slug,title,description,pack_version}]}`; an empty catalogue is a setup/empty-state condition, not permission to invent jobs |
| `POST /interviews` | `{job_id,request_id}` | 201 `InterviewState`; retry of the same owner/request/job → 200 same interview; changed job under the same UUID → 409; unknown job → 404 |
| `GET /interviews/{id}` | Bearer JWT | Authoritative `InterviewState` |
| `POST /interviews/{id}/connect` | `{request_id}` | 200 `ConnectionGrant`; 202 `ConnectionPending`; may replace an older connection when given a new UUID |
| `GET /interviews/{id}/transcript` | Bearer JWT | `TranscriptResponse`; includes partial attempts and accepted Q/A |
| `GET /interviews/{id}/evaluation` | Bearer JWT | `EvaluationResponse`; GET never starts a paid evaluation |
| `POST /interviews/{id}/evaluation` | No body | Explicit retry of a naturally finished failed/expired attempt; 200/202 use the same evaluation envelope; 409 if not eligible; model failure → 502 |
| `POST /interviews/{id}/end` | No body | Returns state after cancelling unfinished work or stopping remaining media for an already naturally finished interview |
| `GET /probes/health`, `GET /probes/ready` | None | Operational probes, not evidence that an interview/voice agent has started; readiness can return 503 |

There is no HTTP audio-upload or answer-submission endpoint, no custom application WebSocket/SSE endpoint, no session-history listing, and no replay/video endpoint in this phase. Voice answers travel over LiveKit audio. Do not use generic LiveKit chat/text input as an undocumented substitute for that path.

## 4. Response types and rendering rules

UUIDs are opaque JSON strings. Treat timestamps such as `expires_at` as timezone-aware ISO date strings, not counters or local timezone strings. The following types describe the fields the consumer relies on; they do not replace runtime validation of untrusted JSON.

```ts
type UUID = string

type LoginResponse = {
  access_token: string
  token_type: 'bearer'
  expires_in: number
}

type InterviewStatus =
  | 'created'
  | 'active'
  | 'paused'
  | 'evaluating'
  | 'completed'
  | 'evaluation_failed'
  | 'cancelled'

type EvaluationStatus = 'not_ready' | 'running' | 'failed' | 'completed'
type DeliveryStatus = 'pending' | 'completed' | 'interrupted' | 'failed'

type Job = {
  id: UUID
  slug: string
  title: string
  description: string
  pack_version: number
}

type Evidence = { answer_id: UUID; quote: string }
type RubricSignal = {
  topic_id: string
  status: 'unobserved' | 'insufficient' | 'assessed'
  skill_ids: string[]
  evidence: Evidence[]
}

type QuestionSummary = {
  id: UUID
  ordinal: number
  kind: 'bank' | 'follow_up'
  text: string
  topic_id: string
  source_answer_id: UUID | null
  delivery_status: DeliveryStatus
}

type InterviewState = {
  id: UUID
  job: Job
  status: InterviewStatus
  revision: number
  question_count: number
  answered_count: number
  follow_up_count: number
  current_question: QuestionSummary | null
  answerable_question_id: UUID | null
  rubric: RubricSignal[]
  topics_covered: string[]
  gaps: string[]
  skills_detected: { skill_id: string; evidence: Evidence[] }[]
  decision: {
    action: 'ask' | 'finish' | 'pause'
    reason_code: string
    reason: string
    topic_id: string | null
    source_answer_id: UUID | null
  } | null
  evaluation_status: EvaluationStatus
  last_error_code: string | null
  can_resume: boolean
}

type ConnectionGrant = {
  interview_id: UUID
  run_id: UUID
  server_url: string
  room_name: string
  participant_identity: string
  participant_token: string
  expires_at: string
  state: InterviewState
}

type ConnectionPending = {
  interview_id: UUID
  run_id: UUID
  status: 'provisioning'
  retry_after_ms: number
}

type Evaluation = {
  strengths: { text: string; answer_ids: UUID[] }[]
  concerns: { text: string; answer_ids: UUID[] }[]
  rubric_scores: { topic_id: string; score: number; answer_ids: UUID[] }[]
  overall_score: number
  insufficient_evidence: boolean
  gaps: string[]
}

type EvaluationResponse = {
  status: EvaluationStatus
  result: Evaluation | null
  error_code: string | null
  can_retry: boolean
}

type TranscriptMessage = {
  id: UUID
  sequence: number
  run_id: UUID
  sdk_message_id: string
  speaker: 'user' | 'assistant'
  question_id: UUID | null
  answer_id: UUID | null
  kind: 'input' | 'question' | 'answer' | 'control' | 'closing'
  processing_status: 'pending' | 'applied' | 'not_applicable'
  text: string
  interrupted: boolean
  text_source: 'stt' | 'tts_aligned' | 'sdk_estimate'
}

type TranscriptResponse = {
  interview_id: UUID
  status: InterviewStatus
  questions: {
    id: UUID
    ordinal: number
    kind: 'bank' | 'follow_up'
    text: string
    topic_id: string
    source_answer_id: UUID | null
    source_quote: string | null
    answer: { id: UUID; text: string } | null
  }[]
  messages: TranscriptMessage[]
}
```

Important interpretation:

- `question_count` counts approved question rows, including an upcoming question. It is not the number of spoken audio chunks.
- `answered_count` counts accepted answers; `follow_up_count` counts answered follow-ups. Do not change either optimistically from captions.
- `current_question` may already describe the next approved question while the prior one is still the answerable question. Render it as upcoming until its ID equals `answerable_question_id`. Resolve another answerable ID from the transcript's question list or a previously fetched summary; do not relabel the candidate's current answer under the new question prematurely.
- Unspoken planned wording can change under the same question ID/revision sequence while additional speech is processed. Replays keep the question ID. SDK speech-handle/stream IDs are not question IDs.
- `assessed` means sufficient evidence to assess a topic, not a positive score. An explicit weak answer can satisfy coverage. Show missing evidence separately from a demonstrated weakness.
- Questions normally stop between six and ten; show “answered N · 6–10 planned” rather than forcing a six-step completion bar or deciding the interview is finished at six.
- Rubric scores are 0–4; the returned overall score is 0–100. Render a legitimate zero score. Do not recompute the backend's weighted total or fabricate a missing evaluation.
- Render transcript/model text through React text nodes, not `dangerouslySetInnerHTML`. These are untrusted strings, not trusted HTML.

## 5. Starting an interview safely

Keep creation and voice admission as two separate operations with separate request UUIDs.

1. Load jobs and select a backend-provided `job_id`.
2. Require valid login. Within an explicit Start/Resume user action, obtain microphone permission before reserving a voice slot. Creating a local microphone track does not publish it until the room is joined.
3. For a new interview, generate one `crypto.randomUUID()` as the **creation** `request_id`. Save the pending `{owner_id,job_id,request_id}` in tab-scoped state and use the same values if the request is retried after an uncertain network result. Disable duplicate Start actions while it is unresolved.
4. POST `/interviews`. Store the returned interview ID in the URL/non-secret tab state, and store the backend state. A retry returning 200 is just as successful as the initial 201. Do not generate another interview because the first request timed out.
5. Generate a different UUID for one explicit **connection attempt**. POST `/interviews/{id}/connect` with it.
6. If the response is 202, wait `retry_after_ms` and retry the same request UUID sequentially. There is no participant token in that response. Do not start a second connect operation or create a Room from it.
7. On 200, store the returned `run_id` and connection grant in memory; register listeners, join the returned server/room with its participant token, and publish the preflight microphone as source `microphone`.
8. Fetch state again after joining. Display “Waiting for interviewer” until the backend supplies/starts the question; a connected room alone is not interview readiness.

Creation and connection UUIDs are idempotency keys, not auth tokens. Use a new creation UUID only for an intentional new interview. Use a new connection UUID only for intentional replacement/resume or recovery from the expired/failed-attempt error. A new connect UUID **replaces the previous voice run**, potentially in another tab. Never rotate it on a generic component render or every polling tick.

Connection failures:

- A network timeout has an unknown server outcome: retry the same UUID first.
- 409 `Connection attempt expired`: refetch state. If `can_resume` is true, let a Resume action obtain a fresh connection UUID for the **same interview**, not a new interview.
- 429 `Voice capacity reached`: no new voice worker was admitted; stop rapid retries, keep the interview, show a capacity message and offer a deliberate retry.
- 503 `Voice service unavailable`: keep the interview ID and show the error. Refetch before resuming; do not pretend a room exists or loop new runs indefinitely.
- A five-minute participant grant can still be unusable after the backend's no-participant deadline: the worker exits after 60 seconds without its candidate. Permission/setup work therefore happens before requesting the grant, and recovery must consult the API rather than trusting only JWT expiry.

## 6. Browser audio and LiveKit

The minimal browser dependency is `livekit-client`; documentation was checked against JS SDK v2.22.4. Its version is independent of Python Agents 1.8.6. A later frontend implementation can add it using `bun add livekit-client@2.22.4` from `frontend/`; this is not a command for the backend implementation phase. The current React/Vite stack need not be replaced with Next.js.

Core SDK operations, to be placed inside the single owned connection controller:

```ts
import { Room, RoomEvent, Track, createLocalAudioTrack } from 'livekit-client'

// Run this from an explicit Start/Resume action, before POST /connect.
const microphone = await createLocalAudioTrack({
  echoCancellation: true,
  noiseSuppression: true,
  autoGainControl: true,
})

// After the API has returned a validated ConnectionGrant:
const room = new Room()

// Register these before connect; audioMount is a UI-owned DOM container.
room.on(RoomEvent.TrackSubscribed, (track) => {
  if (track.kind === Track.Kind.Audio) audioMount.appendChild(track.attach())
})
room.on(RoomEvent.TrackUnsubscribed, (track) => {
  track.detach().forEach((element) => element.remove())
})

await room.connect(grant.server_url, grant.participant_token)
await room.localParticipant.publishTrack(microphone, {
  source: Track.Source.Microphone,
})
```

This illustrates verified SDK calls, not a complete production controller. `grant`, the DOM container and lifecycle/error state belong to the frontend controller described here. It must also implement cancellation, stale-attempt guards, event removal, failure cleanup and the recovery rules below. Do not publish a second microphone via `setMicrophoneEnabled(true)` after publishing this pre-created track. Do not request camera or screen-sharing tracks: the issued grant is audio-only.

Browser output permissions are separate from microphone permissions. Observe `RoomEvent.AudioPlaybackStatusChanged`/`room.canPlaybackAudio`; if playback is blocked, show an **Enable audio** button whose click directly calls `room.startAudio()`. Do not silently continue an interview when the candidate cannot hear it. Audio may arrive before a permission problem is surfaced; provide a way to say “repeat the question” after output is enabled.

Use the published local track to mute/unmute without creating a second track. Mute is not cancellation and does not submit an answer. Show whether the microphone is active and whether the connection/audio output is usable. Do not run browser speech recognition or browser speech synthesis as a competing pipeline.

Handle permission denied, no microphone, device busy/unavailable, insecure origin, and publication failure with an actionable UI error. If setup fails or the user navigates away, stop the local track even if it never published. On an owned-room teardown, stop/unpublish local capture, remove registered event listeners/audio elements and disconnect the room. Do not leave a preflight track recording while a login or retry dialog sits idle.

## 7. One lifecycle owner, including React StrictMode

Use one controller/provider to own the room, microphone, timers, HTTP abort controller, current interview ID, connect UUID, run ID and an incrementing local operation generation. Keep the Room instance stable for that operation. Presentation components read state and issue explicit actions; they do not each create a room.

- Trigger creation/admission from a user action, not an unguarded mount effect. React StrictMode can run setup/cleanup again in development; that must not produce duplicate interviews or new replacement UUIDs.
- Capture the local generation/interview/run in every asynchronous operation. Ignore late HTTP/event callbacks after logout, navigation, cancellation or explicit replacement. Disconnect a room that finishes connecting after its operation became obsolete.
- Use one active state-fetch at a time. If a new invalidation arrives while fetching, mark it dirty and refetch after the request settles. Abort/discard obsolete requests so an older response cannot overwrite a newer run's UI.
- Do not assume `AbortController.abort()` cancels work already received by the backend. Reuse the original idempotency UUID to resolve an unknown create/connect outcome.
- A component unmount only cleans up its owned local resources. It does not POST `/end`; otherwise a StrictMode cleanup or route reload would cancel the persisted interview.
- Preserve the interview ID on reload. Re-login if needed, fetch current state, and offer Resume only when `can_resume` is true. Do not automatically reconnect from an effect that may run twice.

## 8. State updates, captions, and the decision panel

There are three distinct data sources:

| Source | Use | Do not infer |
| --- | --- | --- |
| `GET /interviews/{id}` | Persisted progress, current/answerable question, signals, gaps and selection reason | That every approved question has already been heard |
| `lk.transcription` LiveKit text streams | Optional live user/assistant captions | That a chunk is a complete committed answer or a new question |
| `GET /interviews/{id}/transcript` | Durable Q/A and recorded conversation messages | That planned text or `sdk_estimate` proves an exact audible prefix |

Listen for the application data packet on topic `interview.state`:

```json
{
  "type": "interview.state_changed",
  "interview_id": "<interview UUID>",
  "revision": 7
}
```

The JS `RoomEvent.DataReceived` callback receives `(payload, participant, kind, topic, ...)`. Check topic/type/interview ID and that the callback belongs to the current room operation. Decode UTF-8 JSON safely. Treat the message only as an **invalidation hint** and refetch the authenticated state; do not trust it as a state mutation. A room participant can send data, but cannot rewrite the state returned by the API.

Refetch even when the revision equals the last seen revision: delivery-only updates can share the logical revision. Coalesce bursts instead of issuing an overlapping request for every packet. Reliable packets are not a durable event log and are not buffered through disconnections. Fetch state on initial join, after transport reconnect, and when returning to a visible interview tab. As a safety net while the visible page is in an active nonterminal state, refetch every two seconds with no overlapping request; stop on logout/unmount/terminal state. This is client polling, not an additional server endpoint.

For optional live captions, use the `lk.transcription` **text-stream** API and key partial updates by stream/message identity. The older `TranscriptionReceived` event uses a different, deprecated delivery mechanism and does not receive those text streams. Do not count caption chunks or append every repeated partial as a new persisted turn. Live captions can disappear or be corrected; fetch the durable transcript for results, recovery, and source-answer citations.

The decision panel renders `rubric`, `topics_covered`, `gaps`, `skills_detected`, and `decision.reason` from the API. Resolve evidence through stored answer IDs and highlight quoted text safely. Label these as model-extracted evidence and backend selection rules, not hidden chain-of-thought or proof of a candidate's actual abilities. An interrupted question remains the same question; show its interruption/replay status without creating a new progress step.

## 9. Reconnect, resume, and leaving

Keep LiveKit connection state separate from interview business state:

| Interview status | Frontend behavior |
| --- | --- |
| `created` | Show Start; no voice run assumed |
| `active` | Render authoritative question/progress plus transport/audio indicators |
| `paused` | Show saved progress and Resume; unsaved audio is not recoverable |
| `evaluating` | Stop publishing candidate audio, show evaluation in progress, allow closing audio to finish, then release local media |
| `completed` | Show final transcript/evaluation; no reconnect |
| `evaluation_failed` | Show saved transcript and evaluation-retry UI; no voice reconnect |
| `cancelled` | Show incomplete attempt/partial transcript without a fabricated score; no resume |

For `RoomEvent.Reconnecting`, let the SDK attempt transport recovery; do not call the backend's `/connect` immediately. On `Reconnected`, refetch state/transcript. A successful WebRTC reconnect does not by itself prove the agent lease survived; if the worker has ended or state is paused, offer backend Resume.

For a terminal `Disconnected`, stop local capture, refetch state and show the appropriate Resume/results/error action. An explicit Resume uses a **new connect UUID on the same interview** and can create a new room/run. The backend replays the unanswered question, retaining accepted answers. Do not replay stored audio or re-submit text from a previous caption.

Do not automatically POST a new connection after every disconnection: two tabs could repeatedly replace each other's run. Tell the user that a new connection replaces an existing session; let a deliberate Resume action take control.

Closing the tab or choosing a nonterminal “Leave for now” action releases local media but does not call `/end`; the backend will pause unfinished work. Do not rely on an unload-time fetch to guarantee server cancellation.

For an explicit **End interview** action, stop local microphone capture immediately and POST `/end` while the API token is valid, then reconcile the returned state and disconnect. It is safe to retry the idempotent endpoint after an uncertain response. If cancellation is not confirmed because of a network/auth error, say so; do not label the attempt cancelled locally. The API preserves `evaluating`, `evaluation_failed`, or `completed` if the interview already finished naturally.

Supported standalone voice controls include “repeat the question” and “end the interview.” The backend's control matching is intentionally explicit; do not create a second client-side classifier, assume arbitrary mentions of “stop” cancel the interview, or send synthetic typed control messages in place of speech.

## 10. Evaluation and final transcript

Natural completion is decided only by the backend: minimum question/follow-up counts plus adequate evidence, or the ten-question cap. Seeing six answers is not a reason to call `/end` or `/evaluation`.

When state becomes `evaluating`, fetch `/evaluation`, then poll sequentially every two seconds while the visible results page is active and its status is `running`. Stop polling on logout/navigation, a terminal result, or an error requiring user action; do not create overlapping timers. Also fetch the durable transcript. The audio room may close before evaluation completes, so use HTTP rather than waiting for another room event.

For `status="completed"`, render the stored result, its evidence-backed strengths/concerns, rubric scores, overall score, and any insufficient-evidence warning/gaps. Empty strengths and an overall score of zero are legitimate. If `result` is null, do not invent a score or show a completed-results layout.

For a failed or stalled attempt, expose **Retry evaluation** only when `can_retry` is true. An expired evaluation claim can make retry available even while the envelope still reports `running`; use `can_retry`, not a locally guessed expiry. POST `/evaluation` explicitly once. A 202 means another claim is already running; resume GET polling instead of repeatedly POSTing. A 200 returns the stored/new evaluation; a 502 retains the saved transcript and reports evaluation failure. A later explicit retry may recover it.

Order Q/A by question `ordinal`; order conversation messages by `sequence`. Show approved question text beside its accepted answer for the Q/A view, and interruption/control details separately where useful. Do not merge SDK captions into the stored transcript or replace recorded answers with model summaries. Render an incomplete/cancelled attempt honestly, with no final grade.

## 11. Error recovery reference

| Condition | Correct consumer action |
| --- | --- |
| Microphone permission/device failure | Do not request a voice slot; explain how to fix access, stop any partial capture and allow retry |
| Audio autoplay blocked | Show Enable audio calling `room.startAudio()` from a user gesture; do not claim the question was heard |
| 401 | Clear API credential/private UI, stop capture, log in again, then fetch the preserved interview ID |
| 404 for an interview | Treat as unavailable/not owned; do not probe for another user's data or create an automatic replacement |
| 409 mismatched creation UUID/job | Resolve the pending creation intent; do not silently change the job under the old UUID |
| 409 expired connection attempt | Refetch state and offer Resume with a new connect UUID if allowed |
| 409 on connect in a finished state | Show evaluation/results/incomplete state; do not keep creating voice runs |
| 409 on evaluation retry before natural finish | Continue/restore the interview state, not a forced evaluation |
| 422 | Display request/field errors and correct the payload; do not blindly retry unchanged JSON |
| 429 voice capacity | Keep the interview ID, stop rapid retries and offer a later deliberate attempt |
| 502 evaluation failure | Keep/show transcript; use the evaluation envelope and explicit retry action |
| 503 provider/media/configuration failure | Surface unavailable service, preserve progress, and refetch before attempting recovery |
| Network timeout/aborted HTTP request | Server outcome may be unknown; preserve idempotency keys and reconcile instead of duplicating work |
| Malformed/unexpected payload | Show an integration error; never convert it to an empty success, default score or fabricated transcript |

State `last_error_code` values include `llm_unavailable`, `llm_invalid_output`, `stt_unavailable`, `tts_unavailable`, `voice_dispatch_failed`, `join_timeout`, `evaluation_failed`, and `lease_lost`. Use these for appropriate user guidance, not as a command to change the job, increment a count or generate an answer locally.

## 12. Frontend acceptance checklist

These are checks for the later frontend implementation, not results already obtained:

- Login uses the password form contract; protected calls use the API JWT, and a room token is never used for API authorization.
- All three backend jobs are selectable; an empty/unavailable catalogue is handled without fake data.
- Denied microphone access creates no voice reservation; audio autoplay blocking has a working user-gesture recovery button.
- A double-click, uncertain HTTP result, or StrictMode effect replay does not create duplicate interviews or competing voice runs.
- The 202 provisioning path never reaches `room.connect` without a real token; grant retries preserve the same request UUID.
- One microphone is published with source `microphone`; no camera, screen, browser TTS, or direct provider-key calls are used.
- Current/answerable question rendering remains correct during barge-in, late speech, a repeated question, and delayed snapshot responses.
- Live caption chunks do not increment progress, and delivery-only same-revision hints still trigger a state refresh.
- Temporary transport reconnect does not create a new backend run; final disconnect/reload resumes the existing interview only through a deliberate action.
- Ending early saves an incomplete attempt; natural completion waits for the backend even beyond six questions.
- Evaluation survives loss of the voice room; retry uses `can_retry`, and missing/failed results never become a fabricated score.
- Final transcript/results remain retrievable after reload and login; interrupted text is not presented as an exact heard recording.
- Logout, route teardown, failed connection and explicit replacement release microphone/audio resources and ignore obsolete async callbacks.
- Browser bundles, URLs and logging contain no server/provider secrets, passwords or raw transcript telemetry.

## Documentation sources

The backend HTTP contract is defined by the companion plan and must be checked against the implemented OpenAPI and real responses before frontend integration is declared working. External API references used to prepare the guide:

- [Vite environment variables and public-prefix behavior](https://vite.dev/guide/env-and-mode)
- [LiveKit JS Room lifecycle and audio methods](https://docs.livekit.io/reference/client-sdk-js/classes/Room.html)
- [LiveKit createLocalAudioTrack](https://docs.livekit.io/reference/client-sdk-js/functions/createLocalAudioTrack.html)
- [LiveKit JS room-event callback signatures](https://docs.livekit.io/reference/client-sdk-js/types/RoomEventCallbacks.html)
- [LiveKit camera/microphone publication](https://docs.livekit.io/transport/media/publish/)
- [LiveKit data-packet delivery limitations](https://docs.livekit.io/transport/data/packets/)
- [LiveKit agent text streams and transcription migration](https://docs.livekit.io/agents/multimodality/text/)

No browser UI, provider connection, or frontend SDK example has been executed as part of preparing this guide.
