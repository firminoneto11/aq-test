# AI interviewer backend

> Historical backend-only implementation plan, written before frontend integration and Fly.io deployment. See the root README for current setup and hosted URLs; the application code defines deployed behavior.

## Context

Build only the backend of the AI Interviewer Platform in the existing FastAPI repository: three selectable jobs, hands-free role-grounded interviews, durable full Q/A and a structured final evaluation. Include the first two stretch goals—deterministic interviewer state/reasons and role-specific behavioral/technical question packs—with six-to-ten questions and at least two answer-dependent follow-ups. Before changing application code, publish this plan as `zFiles/interviewer-backend-plan.md` and a detailed consumer guide as `zFiles/frontend-integration-guide.md`; then deliver password/JWT authentication, resumable sessions, and Docker-ready local proof using OpenRouter, Deepgram, ElevenLabs and LiveKit. The owner prefers their own containers/VPS, permits managed media, and has a 48-hour overall assignment deadline; React product implementation and public deployment are not part of this phase.

## Confirmed starting point

- `entrypoints/api/http/fastapi_/controllers/v1/__init__.py` registers only the probes HTTP router; WebSocket and SSE router registries are empty. `routers.py` mounts the v1 app at `/api/v1`.
- `entrypoints/api/http/fastapi_/factory.py` supplies application lifecycle, database/error-tracker state, middleware, and mounted-app dependency context. Reuse this application rather than introduce another HTTP stack.
- `src/infra/database/adapter.py` and `src/infra/database/sqlalchemy_.py` supply the SQLAlchemy database adapter and session machinery. `src/domain/models/base.py` supplies UUID/timestamp/soft-delete model infrastructure. No concrete interview entities or migration revisions were found in the initial inspection.
- `src/infra/llm/adapter.py` supports text completion and schema-shaped output via LangChain integrations for Anthropic, Google, and OpenRouter. There is no existing voice pipeline or interview caller.
- `pyproject.toml` requires Python >=3.14. Importing the existing API previously emitted a LangChain/Pydantic v1 compatibility warning; voice dependency support must be checked before fixing a runtime choice.
- `scripts/startup.sh` invokes `scripts/extraction.py`, which currently truncates `.env` and writes no secrets. Deployment must stop relying on that placeholder extraction path.
- `src/conf/_settings.py` reads the project manifest and Git metadata at import time, including `repo.active_branch.name`. Hosted startup must not depend on a mutable Git checkout.
- `pipecat/` and `pipecat-flows/` reference checkouts mentioned in the Pipecat skill are absent. Third-party behavior must be verified against current upstream documentation/source rather than presumed local references.

## Confirmed product decisions

- Implement the required backend and the first two stretch goals; no product frontend, camera, replay, or analytics implementation.
- Use hands-free streaming STT → LLM → TTS, not STS. Own-hosted containers may connect to managed WebRTC media.
- Provide password login/JWTs and a CLI-provisioned reviewer user. No anonymous-session access or public signup.
- Ship three English-only mid-level role packs: backend engineer, frontend engineer, and data analyst.
- Count six to ten questions with at least two follow-ups grounded in prior answers. After the minimum, continue only for missing/ambiguous rubric evidence, not because demonstrated performance is weak. At ten, finish and explicitly report remaining evidence gaps.
- Resume a disconnected interview from its current unanswered question, preserving committed answers. Do not recover unsaved audio mid-sentence.
- The user superseded the earlier split-model choice: both `INTERVIEW_PLANNER_MODEL` and `INTERVIEW_LLM_MODEL` now default to `openai/gpt-6-luna` through OpenRouter, with independent environment overrides. A synthetic strict-schema follow-up passed in 5.31 seconds and a four-topic evaluation in 11.52 seconds; provider latency varies and this is not proof of the full voice loop. Keep the 20-second live-turn deadline and asynchronous final evaluation. The observed one-question stall had a saved answer still pending with no model-error decision, so a model change alone cannot be assumed to fix it. Deepgram Nova-3 STT and ElevenLabs direct TTS still use separate keys.
- Bound live planning across both the LangChain and OpenRouter SDK layers: structured requests have no implicit retries, and server-controlled corrective attempts share the 20-second turn deadline. The final evaluation may make one evidence-correction attempt; if still invalid, preserve the transcript and expose an explicit owner-only retry instead of a fabricated score.
- Persist transcripts, decision state, and evaluations without automatic expiry. Do not store raw interview audio or video.
- This phase ends with Docker-ready backend code and local real-provider voice proof. Public deployment belongs to subsequent frontend integration; the overall assignment deadline is within 48 hours.

## Voice architecture decision

Use LiveKit Agents orchestration with LiveKit Cloud media and a separately deployed Python voice worker on the user's VPS/container host. Pipecat was evaluated: it gains explicit frame-level composition and media transport choice, but its production VPS setup needs an authenticated bot supervisor/dispatch path and more custom gating to prevent automatic inference from running before interview-state persistence. LiveKit supplies worker dispatch/job isolation/draining and documented pre-response hooks; those reduce custom infrastructure within the selected deadline. Both still require the same durable domain controller, transcript persistence, question packs, and evaluation.

For question control, disable speculative generation and release only a fully formed server-approved question to TTS. Store planned question text separately from committed/interrupted speech. Use application-owned question/answer IDs and recover state from Postgres. Published SDK/source behavior has been researched; installation, provider credentials, native dependency resolution, and real microphone behavior have not been verified in plan mode.

Evidence: https://docs.livekit.io/agents/logic/nodes/ ; https://docs.livekit.io/deploy/custom/deployments/ ; https://docs.pipecat.ai/pipecat/deployment/running-bots-in-production.md ; https://docs.pipecat.ai/pipecat/migration/flows.md .

## Approach

Execute in the order below. Do not register unfinished routes or wire placeholder voice services. Authentication/seed content can be developed independently after the schema exists; HTTP integration depends on the complete application services, and voice integration depends on their transaction contracts.

### 0. Save the plan and frontend integration guide before implementation

The requested root `zFiles/` directory exists and was empty when inspected; `.gitignore` explicitly ignores it. Preserve that tracking rule and do not force-add these documents or overwrite unrelated files. After execution is authorized, before installing dependencies or changing application code, write the current complete approved plan to `zFiles/interviewer-backend-plan.md` and the detailed guide to `zFiles/frontend-integration-guide.md`.

Draft the guide at `local://frontend-integration-guide.md` during planning. At execution, copy its contents to the requested destination; if that session artifact is unavailable, reconstruct the guide from this plan's complete HTTP/voice/state/verification contracts and the mandatory guide contents below. The guide must be self-contained, use repository-relative links to `interviewer-backend-plan.md`, and label the APIs as the planned contract until implementation and verification exist. Do not claim these service routes already run or implement frontend code merely to write the guide.

Required guide sections: frontend/backend responsibility split; Vite public configuration and server-only secrets; password-form login, API bearer versus room-token security and expiry; complete endpoint/status/body reference and TypeScript response contracts; stable create/connect UUIDs and provisioning polling; microphone permissions, browser audio playback and a single LiveKit room lifecycle; live captions versus persisted transcript; current versus answerable question rendering; decision-panel invalidation/refetch; automatic transport reconnect versus explicit backend resume; early cancellation versus natural completion; evaluation polling/retry; error/status recovery; and a frontend acceptance checklist.

The guide must specify exact sequencing for 202/409/429 connection responses, stale HTTP responses and replaced voice runs, microphone denial before capacity admission, React StrictMode duplicate effects, equal-revision state-change hints, five-minute room grants versus 24-hour API JWTs, and token expiry without inventing a refresh endpoint. Include concrete request/response examples and minimal verified SDK integration examples, not a pretend complete UI. Keep these two documents aligned with any contract correction discovered during backend implementation; changing scope still requires a user decision.
Frontend-guide defaults: `VITE_API_BASE_URL=http://localhost:8000/api/v1`, API JWT in memory, non-secret interview/request IDs in owner-keyed tab state, and one controller owning the Room/microphone/HTTP generations. Document SDK v2.22.4 `Room`, `createLocalAudioTrack`, microphone-source publication, remote audio attachment, and user-gesture `startAudio`; installing it belongs to later frontend work. Optional captions use `lk.transcription` text streams, not deprecated `TranscriptionReceived`. Use sequential two-second visible-page state/evaluation polling as a missed-event fallback, stop it on teardown/terminal states, and never auto-POST replacement connections or evaluation retries. These defaults are consumer guidance, not additional frontend implementation in this backend phase.



### 1. Make the runtime usable by both API and voice processes

- Keep Python 3.14 (`.python-version` and Docker base); bound `requires-python` to `>=3.14,<3.15` because the researched Agents release excludes 3.15. Add `livekit-agents[deepgram,elevenlabs,openai,silero]==1.8.6`, `livekit-api`, `argon2-cffi`, and `python-multipart`; resolve and commit compatible transitive pins in `uv.lock`. Do not install Pipecat or add a second voice implementation.
- In `src/conf/_settings.py`, replace import-time `Repo(".")` and working-directory-relative manifest reads with installed `importlib.metadata` for distribution `aq-test`. Preserve the settings/probe fields; make `COMMIT_ID` and `BRANCH_NAME` environment-backed strings defaulting to `"unknown"`. Remove the now-unused GitPython dependency; the only observed `from git` use is this settings module.
- Add secret settings `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `DEEPGRAM_API_KEY`, `ELEVEN_API_KEY`, retain `OPENROUTER_API_KEY`, and add `LIVEKIT_URL`. Read them as `SecretStr` and pass explicit secret strings to SDK constructors: Pydantic reading `.env` does not export environment variables.
- Model settings/defaults: `INTERVIEW_PLANNER_MODEL="openai/gpt-6-luna"`, `INTERVIEW_LLM_MODEL="openai/gpt-6-luna"`, `DEEPGRAM_MODEL="nova-3"`, `ELEVEN_MODEL="eleven_turbo_v2_5"`, `ELEVEN_VOICE_ID="hpp4J3VqNfWAUOO0d1Us"`. English is fixed. Configure `VOICE_MAX_ACTIVE=2`, `VOICE_LEASE_SECONDS=30`, `VOICE_HEARTBEAT_SECONDS=10`, `LLM_TIMEOUT_SECONDS=20` for live turns, and `EVALUATION_TIMEOUT_SECONDS=75` for final grading.
- Preserve `LLMPort`; construct separate `LLMAdapter(provider="openrouter", model=...)` instances for planning and evaluation unless the configured model IDs match. Pass `openrouter_provider={"require_parameters": True, "data_collection": "deny"}`. For OpenRouter structured output request `strict=True`, validate parsed results against domain schemas, and do not add an automatic adapter retry underneath the application-owned one corrective invalid attempt. Do not change unrelated providers' completion behavior.
- Validate required speech/LiveKit secrets at voice-worker startup and when admitting a voice connection, not when importing the HTTP app or running ordinary tests. Require a non-default JWT secret of at least 32 bytes when starting the credential-issuing API; tests supply an explicit testing secret. Preserve DB settings `DB_DRIVER`/`DB_SERVER` and existing health/readiness response contracts.

### 2. Establish durable records and versioned role packs

Add `src/domain/models/user.py` and `src/domain/models/interview.py`, importing their models from the existing `src/domain/models/__init__.py` so Alembic and tests see the same metadata. Reuse `TimeStampedBaseModel`. No account, job, interview, or transcript model currently exists.

Store these entities, with foreign keys and database uniqueness, not only in worker memory:

| Entity | Required fields beyond the existing UUID/timestamps |
| --- | --- |
| `User` | normalized unique `email`, `password_hash`, `is_active` |
| `Job` | unique `slug`, `title`, `description`, integer `pack_version`, validated `pack` JSONB |
| `Interview` | `user_id`, `job_id`, immutable `job_snapshot` JSONB, `status`, logical `revision`, approved `current_question_id`, separate `answerable_question_id` (last begun), latest `signals` JSONB, `finish_reason`, `evaluation` JSONB/null, `last_error_code`/null, evaluation claim UUID/expiry, current voice run UUID/worker job ID/room/connect-request UUID/lease expiry, `connection_state=provisioning|ready|failed`, grant issue/expiry timestamps, create-request UUID |
| `InterviewQuestion` | `interview_id`, `ordinal`, `kind=bank|follow_up`, `topic_id`, nullable `bank_question_id`, nullable `source_answer_id`/`source_quote`, approved `text`, `delivery_status=pending|completed|interrupted|failed` |
| `InterviewAnswer` | unique `question_id`, `interview_id`, aggregated accepted `text`, integer `revision` |
| `TranscriptEntry` | `interview_id`, monotonically assigned `sequence`, `run_id`, `sdk_message_id`, `speaker=user|assistant`, question ID captured at user speech start, nullable answer ID, `kind=input|question|answer|control|closing`, `processing_status=pending|applied|not_applicable`, `text`, `interrupted`, `text_source=stt|tts_aligned|sdk_estimate` |
| `InterviewDecision` | `interview_id`, unique logical `revision`, `action=ask|finish|pause`, nullable question/topic/source-answer IDs, `reason_code`, application-rendered `reason`, complete signal snapshot |

Constraints: unique `(user_id, create_request_id)`, unique `(interview_id, ordinal)`, ordinal between 1 and 10, unique `(interview_id, run_id, sdk_message_id)` for transcript deduplication, and unique transcript sequence within an interview. A follow-up must have a source answer from an earlier question in the same interview; a bank question must have a valid pack ID and no source answer. Enforce cross-row rules in the application transaction. Define the interview/current-question and answerable-question FKs as named `use_alter=True` constraints referencing `InterviewQuestion`; create those constraints after both tables in the migration.

`Interview.status` literals: `created`, `active`, `paused`, `evaluating`, `completed`, `evaluation_failed`, `cancelled`. Voice admission and `can_resume` are allowed only for `created|active|paused`. A disconnected unfinished call becomes `paused`, not `completed`. `/end` may cancel only those unfinished states; for `evaluating|evaluation_failed|completed` it stops remaining media without changing natural completion or blocking evaluation/retry. Evaluation failure retains all transcript/decision data.

Add `src/domain/interviews.py` for Pydantic schemas/enums and `src/app/job_packs.py` for the three typed v1 packs defined below. Add `manage.py seed-jobs` in `entrypoints/cli/commands.py`: transactionally upsert by slug, preserving IDs, so repeated seeding does not duplicate jobs. Copy the entire job/pack into an interview when created; subsequent seeding never changes an existing interview's questions or rubric.

Commit initial Alembic schema revision(s) under `src/infra/database/migrations/versions/`; schema creation must work through `uv run alembic upgrade head` on an empty Postgres 18 database. Keep seed data in the explicit seed command, not runtime `create_all` or an unversioned migration importing mutable application code.

### 3. Implement password login and session ownership

Extend `AuthUseCase` in `src/app/usecases/auth.py`: retain `authenticate(jwt_token: str) -> UserSchema`, add `async login(email: str, password: str) -> TokenSchema` and `async create_user(email: str, password: str) -> UserSchema`, and inject `SqlDBClientPort` where account lookup is needed. Add a small `src/infra/passwords.py` hashing service using documented `argon2.PasswordHasher` APIs; run hashing/verification off the async event loop. Normalize email by stripping/lowercasing, require a nonempty email-shaped username and a 12–128-character provisioned password, and never store/log plaintext credentials.

Issue HS256 JWTs with required `sub` (user UUID), `email`, `iat`, `exp`, `iss="aq-interviewer"`, `aud="aq-interviewer-api"`. Lifetime: 86400 seconds; expiry requires login again. Require those claims, issuer, audience and the configured algorithm during decode; do not accept unsigned tokens or algorithm negotiation from the token header. Unknown email, wrong password and disabled-user login all return `401 {"detail":"Invalid credentials"}`; run a dummy hash verification for an unknown email to avoid an immediate timing distinction.

Change `src/domain/schemas.py::UserSchema` to `{id: UUID, email: str}` and remove the unused `permissions` requirement. Map decoded JWT `sub` explicitly to `UserSchema.id` as a UUID; do not retain `UserSchema(**payload)`, because the token has no `id` field. LSP found consumers only in `src/app/usecases/auth.py` and `entrypoints/api/http/fastapi_/utils/state.py`; update them together. The dependency is the sole existing instantiator of `AuthUseCase`. Make `use_auth` async and return the active DB user found by decoded `id`, not the token's email alone. Decode only HS256; preserve expired/invalid/missing-claim failure details and `GenericException` serialization. Point `OAuth2PasswordBearer` at `/api/v1/auth/token`, replacing the external placeholder.

Use `DBClientDep` for the auth lookup and close its bounded session before returning the principal; do not keep a yielded request transaction open throughout a model/media operation.

Add `manage.py create-user --email <email>` using Typer's hidden, confirmed password prompt. Duplicate email is an error/nonzero exit, not an implicit password reset. No public signup, refresh-token store, role-management UI, or password-reset implementation is needed for the manually provisioned reviewer account.

### 4. Implement deterministic progression, evidence, and evaluation

Add a pure `InterviewPolicy.next_step(snapshot: InterviewSnapshot) -> NextStep` in `src/domain/interviews.py`; add `InterviewService` in `src/app/usecases/interviews.py`, constructed with the existing DB and LLM ports. Keep ORM transactions in the use case; no second repository abstraction is required for these seven concrete entities.

Application service interfaces (implement fully; response types are the schemas specified below): `create(owner_id: UUID, job_id: UUID, request_id: UUID) -> tuple[InterviewState, bool]` returns state plus whether it was created; `get_state(owner_id: UUID, interview_id: UUID) -> InterviewState`; `get_transcript(owner_id: UUID, interview_id: UUID) -> TranscriptResponse`; `get_evaluation(owner_id: UUID, interview_id: UUID) -> EvaluationResponse`; `record_turn(interview_id: UUID, run_id: UUID, sdk_message_id: str, captured_question_id: UUID | None, text: str) -> RecordedTurn`; `prepare_reply(interview_id: UUID, run_id: UUID, is_current: Callable[[], bool]) -> ApprovedReply | None`; and `evaluate(interview_id: UUID) -> EvaluationResponse`. All are async. `RecordedTurn` carries entry UUID, captured question UUID, kind (`answer|repeat|stop|prelude|duplicate`) and logical revision; duplicate/prelude outcomes produce no new speech. `ApprovedReply` carries nullable question UUID, `kind=question|control|closing`, and exact text. The worker supplies an input-epoch/run predicate to `prepare_reply`; the service checks it before commit and the node rechecks before output. Controllers authorize owner access before the privileged evaluation command. `VoiceSessionService.connect(owner_id, interview_id, request_id)` and `.end(owner_id, interview_id)` return the connection union and `InterviewState` respectively.

Count questions by accepted answer rows joined to their question, not by STT chunks, tool calls, speech handles, greetings, or replays. A substantive `"I don't know"`/decline to answer is an answer and can provide negative evidence. Persist finalized user utterances, including controls, exactly once. Classify spoken controls in code before any model call: lowercase/strip, collapse whitespace, and remove trailing `.?!`; an exact whole-utterance match to `end the interview`, `stop the interview`, `cancel the interview` (also with a leading `please `) cancels. Exact `repeat`, `repeat that`, `repeat the question`, `clarify the question`, `can you repeat the question`, `could you repeat the question`, `can you clarify the question`, `could you clarify the question` (also with a leading `please `) replays the answerable question without a new count or answer hint. Empty text does not create an answer; all other substantive speech is answer data, including quoted instructions or a sentence merely containing `stop`. The model cannot authorize cancellation or discard an answer. Mark control inputs applied in the same transaction as their action; explicit cancellation saves partial text and does not fabricate an evaluation.

Use this deterministic schedule to ensure both minimum counts and category coverage:

1. Question 1: a bank question from the role's `ownership` topic.
2. Question 2: generated follow-up on answer 1.
3. Question 3: a bank question from the role's first technical topic.
4. Question 4: generated follow-up on answer 3.
5. Question 5: a bank question from the second technical topic.
6. Question 6: a bank question from the remaining behavioral topic.
7. After question 6, if all four rubric topics are `assessed`, finish with `reason_code="coverage_complete"`. Otherwise follow up on a gap topic's latest answer. Choose the gap with the fewest post-minimum probes so far, then descending rubric weight, then pack topic order; this prevents one vague topic from consuming every remaining slot. Never repeat a bank ID. After question 10, finish with `reason_code="question_limit"` and report remaining gaps. Never extend merely to improve a weak score.

The LLM selects among the eligible bank IDs, but bank question wording is read verbatim from the pack. It generates follow-ups only for the source answer/topic authorized by the policy. Require a verbatim nonempty `source_quote` from that stored answer plus the source answer ID; include that quoted detail in the candidate-facing follow-up (for example, `You mentioned "..." — ...`). Limit approved questions to 450 characters and one question; no markdown, multi-question bundles, code blocks, answer hints or invented candidate facts.

Each rubric signal is `{topic_id, status:"unobserved"|"insufficient"|"assessed", skill_ids:[], evidence:[{answer_id, quote}]}`. Each versioned topic's finite allowed skill IDs are listed below. Every cited answer must belong to this interview and to a question with this same topic; each quote must be a verbatim substring of that answer. `assessed` requires evidence, including explicit negative evidence; it does not mean competent. A detected skill requires positive demonstrated evidence, not merely mention of its name. Reject unsupported IDs/quotes; derive `topics_covered`, `gaps`, and `skills_detected` in code from these validated signals. Missing evidence remains a gap rather than a claimed lack of ability.

Use one structured OpenRouter assessment/planning call per normal candidate turn: `TurnPlan` contains `signal: RubricSignal` for the just-answered question's topic, nullable `bank_question_id`, nullable `follow_up_text`, and nullable `source_quote`; no intent/cancellation field is allowed. Re-assess that topic using its accumulated saved answers and preserve other topics' stored signals unchanged. Supply the deterministic schedule/gap rule and eligible IDs; validate the proposed question against the policy using the newly validated signal. First-question selection is a separate ID-only call. All nested Pydantic models use `extra="forbid"` and required nullable fields. Allow one corrective attempt within a 20-second total deadline for an invalid schema/business rule; timeout/refusal or a second invalid result pauses with a safe code. Never substitute a canned follow-up and label it answer-dependent.

Post-minimum turns use two stages only when a gap remains: first assess the just-answered topic with nullable question fields set to null, then let the server choose the next gap and source answer. A second narrowly scoped structured call proposes its targeted follow-up; if the generated question omits the cited verbatim quote, the server includes that quote before the generated question and validates the assembled question as usual. No second call is made when coverage is complete or the question limit is reached. Both stages share the 20-second live-turn deadline.

Completed-turn hook transaction: save/deduplicate the raw finalized user entry with the question captured at speech start; increment logical revision; classify controls deterministically; for normal input, create or append the one answer for that question immediately, linking its transcript entries. Mark controls `applied`; normal entries remain `pending` until their evidence/next-step assessment commits. Answer text is the ordered concatenation of its linked finalized entries, not model-rewritten text. Duplicate callbacks return the previously saved outcome without another append/count. No provider call occurs inside this hook.

At each new voice run, clear `answerable_question_id` until a question begins. Speech captured with no answerable question (for example a pre-question greeting) is saved as applied control text, never as an answer. In natural-finish states, later speech is likewise transcript/control only and cannot mutate the frozen evaluation input.

Question planning runs inside the overridden `llm_node`, after the quick hook has returned. Take the pending-input/answer snapshot, logical revision and local input epoch; call OpenRouter outside the DB transaction; then lock the interview and check run/revision/epoch before atomically saving signals, marking the assessed inputs applied, recording the decision, and storing the next approved question or natural finish. A cancelled/failed generation leaves accepted text and pending markers recoverable. On resume, assess pending entries before deciding which question to speak; never infer pending status merely from a null answer ID.

Use an earlier synchronous `user_state_changed` speaking callback to increment a per-job input epoch and capture `answerable_question_id`, before any completed-turn hook is awaited. Agents 1.8.6 serializes completed-turn hooks, so moving network work into `llm_node` plus the epoch check is required: a later hook alone cannot invalidate an earlier hook that is still waiting on a model. Recheck the epoch before commit and before returning speech; a stale generation emits no question. A later completed turn may replan, but a VAD-only false start need not produce one. For an accepted answer, wait for the original SDK reply handle and a quiet grace interval, then recheck the stored pending answer under the controller mutex and run/epoch/finalized-turn fences before speaking one approved question. Do not count interim STT as an answer or promise unsaved audio/text survives a crash.

Keep approved `current_question_id` separate from `answerable_question_id`. Associate a speech handle with its approved question before output; when that handle first enters speaking, persist that question as answerable. While user speech is in flight, defer starting the next question. Extra speech that began before the next question began appends to the preceding answer; re-assess it and revise the same never-begun pending question row/ordinal rather than creating another. Once a question has begun, its text is immutable and new speech is bound to it. Finalized transcript entries retain their captured question ID across async work and reconnect.

Keep one per-job controller mutex and DB uniqueness/revision fencing; do not share an `AsyncSession`. Logical revision changes for saved input, answer/decision/run/state changes, not heartbeats or immutable delivery observations. All progress/evaluation writes verify their current run/claim; late old-run assistant observations may be appended under their original IDs but cannot change the current question, answer, or delivery status.

Persist application-generated reason codes/text: `initial_topic`, `required_follow_up`, `required_topic`, `evidence_gap`, `repeat_requested`, `coverage_complete`, `question_limit`, `provider_unavailable`, `user_stop`. Reasons state the actual rule and relevant topic/source answer, not chain-of-thought. Safe `last_error_code` values are `llm_unavailable`, `llm_invalid_output`, `stt_unavailable`, `tts_unavailable`, `voice_dispatch_failed`, `join_timeout`, `evaluation_failed`, and `lease_lost`; clear the previous error when its recovery operation succeeds.

On natural completion, atomically set `evaluating`, freeze the final validated signal snapshot, and claim an evaluation attempt using a duration of `EVALUATION_TIMEOUT_SECONDS + 45` (120 seconds by default). Run a separate asynchronous schema-constrained OpenRouter call using the configured evaluation model over saved Q/A and the rubric, with a 75-second total evaluation deadline by default. Public `Evaluation` is `{strengths:[{text,answer_ids}], concerns:[{text,answer_ids}], rubric_scores:[{topic_id,score:0..4,answer_ids}], overall_score:0..100, insufficient_evidence:bool, gaps:[topic_id]}`. The model proposes supported strengths/concerns and scores only; construct exactly four rubric rows in code, force unassessed topics to zero demonstrated points, derive `gaps`/`insufficient_evidence` from the frozen signals, and compute `overall_score = round(sum(weight * score / 4))`. Verify all citations against this interview and each rubric row's topic. An explicitly labelled gap may have no citation; a competence claim may not. Allow empty strengths. Judge answer content, not accent, emotion, demographic traits or supposed personality.

Compare-and-set both completion and failure on the current evaluation claim UUID and `status="evaluating"`; displaced/expired attempts discard their result and cannot overwrite a newer attempt. Error becomes `evaluation_failed` with safe code and preserved transcript. An expired claim can be replaced; a current claim returns the documented 202 evaluation envelope. Completed retries return the same 200 envelope/result without another model call. Natural finish remains terminal for voice even when evaluation fails.

### 5. Expose backend contracts for the future frontend

Add `auth.py`, `jobs.py`, and `interviews.py` controllers under the existing `entrypoints/api/http/fastapi_/controllers/v1/`, registered in its `__init__.py`. Reuse mounted-app state/dependencies and `GenericException`; validation failures use FastAPI's normal 422. Missing and foreign-owned interview IDs both return 404. All routes except probes, login, and the sample-job catalogue require bearer auth.

| Method and path below `/api/v1` | Contract |
| --- | --- |
| `POST /auth/token` | OAuth2 form `username` (email), `password`; `{access_token,token_type:"bearer",expires_in:86400}` |
| `GET /auth/me` | `{id,email}` of active user |
| `GET /jobs` | `{jobs:[{id,slug,title,description,pack_version}]}` sorted by slug; seed absence yields an empty list, never fake jobs |
| `POST /interviews` | `{job_id:UUID,request_id:UUID}` → 201 `InterviewState`; duplicate owner/request with same job returns 200 existing state, mismatched job returns 409; unknown job returns 404 |
| `GET /interviews/{id}` | Owner's `InterviewState` |
| `POST /interviews/{id}/connect` | `{request_id:UUID}` → 200 `{interview_id,run_id,server_url,room_name,participant_identity,participant_token,expires_at,state}` only when ready; an in-progress same-request retry returns 202 `{interview_id,run_id,status:"provisioning",retry_after_ms:500}` without a token; grants expire 300 seconds after original issuance |
| `GET /interviews/{id}/transcript` | `{interview_id,status,questions:[{id,ordinal,kind,text,topic_id,source_answer_id,source_quote,answer:{id,text}|null}],messages:[TranscriptEntry]}` in ordinal/sequence order |
| `GET /interviews/{id}/evaluation` | `{status:"not_ready"|"running"|"failed"|"completed",result:Evaluation|null,error_code:null|string,can_retry:bool}`; no model call on GET |
| `POST /interviews/{id}/evaluation` | Retry only a naturally finished failed/expired attempt; 409 before natural completion; 200/202 use the same envelope as GET (202 for a current claim); model failure returns 502 with safe detail and persisted failed status |
| `POST /interviews/{id}/end` | Cancel only `created|active|paused`, fence the run and stop media; idempotent for cancelled sessions. For `evaluating|evaluation_failed|completed`, stop remaining media but preserve natural-finish/evaluation state |

`InterviewState` is `{id,job:{id,slug,title,description,pack_version},status,revision,question_count,answered_count,follow_up_count,current_question:null|{id,ordinal,kind,text,topic_id,source_answer_id,delivery_status},answerable_question_id:null|UUID,rubric:[RubricSignal],topics_covered:[topic_id],gaps:[topic_id],skills_detected:[{skill_id,evidence}],decision:null|{action,reason_code,reason,topic_id,source_answer_id},evaluation_status,last_error_code,can_resume}`. `follow_up_count` is answered follow-ups; `question_count` is approved question rows, not spoken repetitions. Current approved text may precede audio; `answerable_question_id` identifies the question actually begun. Full generated plans/private prompts are not exposed. Natural-finish states set `can_resume=false`.

For the consumer guide's DTOs, serialize UUIDs as strings and `expires_at` as a timezone-aware ISO timestamp. `InterviewState.evaluation_status` uses the same `not_ready|running|failed|completed` enum as the evaluation envelope; derive it from interview/evaluation state. The transcript response explicitly exposes entry UUID, sequence, run/SDK IDs, speaker, nullable question/answer IDs, kind, processing status, text, interruption flag and text source, as defined by the entity contract; the frontend must not depend on extra ORM fields.

Do not add a new SSE service merely for the panel. After committed progress or delivery changes, publish `{"type":"interview.state_changed","interview_id":...,"revision":...}` reliably on LiveKit topic `interview.state`, without transcript or credentials. This is an invalidation hint: refetch GET state even if a delivery-only notification has the same logical revision. Reconnect always fetches the durable snapshot; media data messages are not replayable storage. HTTP JSON/bearer contracts remain usable without a frontend.

### 6. Admit and recover voice connections without duplicate workers

Add `VoiceGatewayPort` to `src/app/ports.py` and implement it in new `src/infra/voice/gateway.py`: `async create_room(room_name: str) -> None`, `async dispatch(room_name: str, interview_id: UUID, run_id: UUID) -> str`, `issue_token(room_name: str, participant_identity: str, expires_at: datetime) -> str`, and `async close_room(room_name: str) -> None`. Use the supported LiveKit server API; closing an already absent room succeeds. No equivalent media gateway exists in the repo.

Add `VoiceSessionService` in `src/app/usecases/voice_sessions.py`. For a new connect-request UUID, serialize capacity admission with one PostgreSQL transaction advisory lock; count nonexpired voice leases, excluding the interview being replaced. At the configured cap return `429 {"detail":"Voice capacity reached"}` without dispatch or provider charges.

Authorize the owner and require `created|active|paused`. Allocate a fresh `run_id`, reserve its 30-second lease and persist `connection_state="provisioning"` plus its request UUID. Room name is `interview-{interview_id}-{run_id}` and participant identity is `candidate-{interview_id}`; never use emails in media metadata. A new request intentionally replaces the previous connection and fences its progress writes before closing its room. A same-request retry in `provisioning` returns the documented 202 without dispatching; only `ready` may return a token.

If the owned interview is `evaluating|evaluation_failed|completed|cancelled`, return `409 {"detail":"Interview cannot be resumed"}` without changing its state or allocating a lease. This terminal-state conflict is distinct from an expired connection-attempt UUID; the frontend must fetch/render its result or incomplete state rather than cycle new connect requests.

Create the room and explicitly dispatch `agent_name="interviewer"` with metadata JSON containing only `interview_id` and `run_id`. After dispatch succeeds, compare-and-set that run to `ready` and persist grant issue/expiry timestamps (300 seconds). Generate a candidate token with `room_join=True`, the exact room, `can_publish=True`, `can_subscribe=True`, `can_publish_data=True`, and `can_publish_sources=["microphone"]`; no admin/video/screen grants. Ready same-request retries return the same run/permissions without extending original expiry; a re-signed token may have at most the remaining lifetime. If the lease, provisioning deadline or grant expires, or that attempt failed, same-request retry returns 409 `"Connection attempt expired"` and requires a new UUID. Grant expiry does not end an already connected participant.

Do not hold a DB transaction over network calls. If creation/dispatch fails or readiness loses its run fence, close only the superseded/failed room, set that still-current attempt failed, release its lease, pause unfinished state and return 503 `"Voice service unavailable"`. If the process dies while provisioning, lease expiry makes it recoverable with a new request; no retry invents a usable grant. A worker starts paid processing only after confirming that its reserved run remains valid.

At job entry, atomically claim the reserved run with its unique LiveKit job ID; another job claiming the same run is rejected. Renew its lease every ten seconds only while the run/job IDs match and the prior lease is unexpired. Before media/model startup, require the expected candidate to join within 60 seconds of job entry; this deadline is independent of `user_away_timeout`, which does not cover a missing participant. If nobody joins, close the room, release that run, mark it failed/paused, and terminate. Do not reserve both worker slots indefinitely for abandoned grants.

Lost lease, replacement, cancellation, disconnect or idle termination stops the old worker. Release only its own lease and pause only unfinished state; never overwrite a newer run or natural finish. Crash recovery uses lease expiry and a new connect UUID to restore saved answers/pending input and replay the unanswered question. Progress/model results from an old run are discarded; immutable late transcript observations cannot change new-run delivery/progression. A room-close network failure remains visible as a safe media error while the fenced worker shuts itself down on its next heartbeat.

### 7. Implement the LiveKit voice worker against verified SDK contracts

Add new `entrypoints/voice/agent.py` as the separate composition root and `src/infra/voice/interviewer.py` containing `InterviewerAgent`. The entrypoint uses `AgentServer`, registers `@server.rtc_session(agent_name="interviewer", ...)`, loads dispatch metadata/DB state, and constructs its own `SqlDBClientAdapter`. Do not bind this worker into the FastAPI lifespan or launch one per Gunicorn worker.

Construct `AgentSession` with `deepgram.STT(model="nova-3", language="en", api_key=...)`, `elevenlabs.TTS(model=settings.ELEVEN_MODEL, voice_id=settings.ELEVEN_VOICE_ID, api_key=...)`, Silero VAD, and a real `openai.LLM.with_openrouter(model=..., api_key=...)`. Use LiveKit's built-in local audio `inference.TurnDetector(version="v1-mini")` as `turn_handling.turn_detection`; retain `preemptive_generation={"enabled":False}` and VAD interruption mode. The mini model is part of the existing Agents/`livekit-local-inference` runtime, needs CPU and about 244 MB resident preload, and avoids the managed full-model dependency on the own-hosted worker. This improves detection of unfinished turns but does not make long pauses unlimited. Keep `use_tts_aligned_transcript=True` and `user_away_timeout=120`. Start with `record=False`; disable Cloud project Agent observability/recordings too. Direct-provider retention remains subject to the chosen accounts; do not claim an application-side no-recording setting guarantees provider zero retention.

The configured LiveKit LLM is required by Agents 1.8.6 even with an overridden `llm_node`; setting it to `None` bypasses completed-turn processing. The override below intentionally uses the existing application LLM adapter for structured planning, not LiveKit's autonomous default conversation generation. This is a supported real provider object, not a dummy or mocked model.

- `async on_user_turn_completed(self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage) -> None`: perform only the fast input/control/answer transaction described above, create a reply-association slot keyed by `new_message.id`, and return normally. No LLM call in this hook. Do not treat interim/final STT segment events as an accepted complete answer and do not raise `StopResponse` to finish.
- `async llm_node(self, chat_ctx, tools, model_settings) -> str | None`: perform pending assessment/planning through `InterviewService`, check the input epoch/run/revision, persist the approved result, fill that turn's reply slot with question ID/kind, and only then return its exact text. Controls replay without model calls. On natural finish return the fixed closing statement `Thank you. The interview is complete. Your evaluation will be available shortly.` once; later/cancelled/stale outputs return `None`. Never call the default autonomous LLM node.
- On initial connect/resume, assess saved pending input if present, otherwise restore/select the pending question, and persist it before `session.say`. Set a pending say-association before calling `say()`: its `speech_created` event occurs inside that call, before it returns. Subsequent turns use `llm_node`, not both `say` and a generated reply for the same question.
- Register synchronous callbacks and retain their async writes. `speech_created(source="generate_reply")` binds the oldest unbound completed-turn reply slot to the handle; correlate the node to that slot by its user-message ID. There are no tools, speculative replies or manual `generate_reply` calls in this worker, so each normally returned completed-turn hook supplies exactly one such slot. The slot starts without a question and is filled before node text reaches TTS. A cancelled/empty unfilled slot is not an audible question. For explicit `say`, bind the predeclared association immediately.
- At agent state `speaking`, use public `session.current_speech` and its bound slot to update the in-memory/persisted answerable question before accepting a new utterance. Defer question output while input is active and check the early input epoch. Do not look up whichever global current question happens to exist when a delayed completion callback runs.
- Observe each handle to completion and inspect `.exception()`, `.interrupted` and `.chat_items`. Completion tasks persist assistant items under the bound question/kind after association is available; user entries are already handled in the completed-turn hook and must not be inserted twice from `conversation_item_added`. Store approved text separately from observed speech. Prefer ElevenLabs aligned text and mark it `tts_aligned`; absent alignment is `sdk_estimate`, not an exact-heard-prefix claim. An interruption before output can have no assistant item.
- A candidate's answer to an interrupted but displayed question can be accepted once and keeps that question ID. Interruption alone never increments counters. Recover an unanswered interrupted question by replaying it; recover an answered question by advancing from its saved answer.
- Start/retain the bounded evaluation task after natural completion; send state-change hints when it persists. Use `shutdown_process_timeout=120`, `session_end_timeout=120` and a 90-second final-speech/evaluation grace on `AgentServer`; await retained tasks in shutdown callbacks. Crash recovery uses the durable 120-second default evaluation claim and explicit retry, not in-memory state.
- Configure the worker's load function from active jobs divided by `VOICE_MAX_ACTIVE`, `load_threshold=1.0`, and `num_idle_processes=1`; the database admission limit remains authoritative. On 120 seconds of inactivity, pause and close instead of asking extra unsolicited questions. STT/TTS errors pause with saved state; never continue counting while the candidate cannot hear the interviewer.

Verified SDK references for this composition: [OpenRouter](https://docs.livekit.io/agents/models/llm/openrouter/), [Deepgram](https://docs.livekit.io/agents/models/stt/deepgram/), [ElevenLabs](https://docs.livekit.io/agents/models/tts/elevenlabs/), [Agents 1.8.6 completed-turn ordering](https://github.com/livekit/agents/blob/livekit-agents%401.8.6/livekit-agents/livekit/agents/voice/agent_activity.py#L2707-L2879), and [AgentServer lifecycle options](https://github.com/livekit/agents/blob/livekit-agents%401.8.6/livekit-agents/livekit/agents/worker.py#L288-L355). These APIs were inspected during planning; installed-dependency and live-provider behavior must still be proved by the execution checks.

Configure `ASGIFactory` state and `types_.py::_CustomAppState` with the completed services, copying them into mounted apps through `_update_sub_mount_state`. Keep provider clients long-lived and close them in their owning lifecycle. To preserve credential-free API imports/tests/probes, HTTP-side services may have an explicitly unconfigured optional LLM/gateway until matching keys are present; only provider-requiring operations call a `require_*` guard returning 503 `"LLM service unavailable"`/`"Voice service unavailable"`. This is a visible configuration error, not a fake provider. Tests inject real service interfaces with deterministic test doubles; the voice worker requires all keys at startup and never runs with missing providers. Its DB adapter closes independently.

### 8. Package one backend image with separate service commands

Update `Dockerfile` to build/install the locked Python project and support API, migration/seed, and voice commands. Add `.dockerignore` excluding `.git`, `.env*`, virtualenvs, node_modules, frontend output and local caches so credentials cannot enter the build context. Delete the obsolete `scripts/extraction.py` after removing its only caller; change `scripts/startup.sh` to simply exec `python manage.py prod` without sourcing or modifying `.env`.

Add `compose.yaml` with `db` (`postgres:18`, named persistent volume, healthcheck, local binding `127.0.0.1:5432`), one-shot `migrate` (`uv run alembic upgrade head` followed by `uv run python manage.py seed-jobs`), `api` (`HOST=0.0.0.0`, `PORT=8000`, local published port 8000), and `agent` (`uv run python -m entrypoints.voice.agent start`). API/agent wait for the successful migration service. Secrets enter via environment/`env_file`, never build args or committed credentials. Download Silero's model assets in the image using the agent entrypoint's `download-files` command; importing that command must not require DB/provider connections.

Provide `.env.example` keys for the DB, JWT, LiveKit, OpenRouter, Deepgram and ElevenLabs settings above; no actual key material. Set `ALLOWED_ORIGINS` to the eventual frontend origin through environment, with `http://localhost:5173` for local integration, rather than using permissive production CORS. Do not expose Postgres publicly or embed a frontend server in this backend deliverable.

## Role-pack content and scoring contract

All packs have `version=1`, `seniority="mid"`, `language="en"`, and topics in the order shown. Topic order breaks equal-weight gap ties. Bank IDs are `<job-slug>.<topic-id>.a` and `.b`; do not generate an unbounded bank at startup. Each topic has the two exact selectable prompts below, so there are eight bank questions per job plus dynamically generated follow-ups.

### Backend engineer

`slug="backend-engineer"`, title `Backend Engineer`, description `Build reliable APIs and data-backed services, collaborate on trade-offs, and operate systems in production.`

| Topic / category / weight | Prompt A | Prompt B |
| --- | --- | --- |
| `ownership` / behavioral / 20 | How did you handle a trade-off with another team while owning a backend change? | What did you do when a backend delivery you owned was at risk? |
| `api_design` / technical / 30 | How would you design an idempotent order-creation endpoint that a client may retry? | What contract would you use for paginating a frequently changing resource collection? |
| `data_consistency` / technical / 30 | How would you prevent two concurrent requests from reserving the last item in stock? | What would you investigate when a previously fast SQL query starts timing out as data grows? |
| `reliability` / behavioral / 20 | How did you diagnose a production incident when the initial symptom pointed to the wrong service? | What did you learn from a backend failure that changed how your team operates the service? |

Rubric evidence: ownership of actions/trade-offs; coherent API/error/idempotency contracts; correct data integrity/query reasoning; incident diagnosis and prevention supported by actual actions.

Allowed skill IDs by topic: `ownership=[tradeoffs,collaboration]`, `api_design=[api_contracts,idempotency]`, `data_consistency=[transactions,query_reasoning]`, `reliability=[diagnosis,prevention]`. Prefix serialized skill IDs with the topic (`api_design.idempotency`); apply that convention to every pack below.

### Frontend engineer

`slug="frontend-engineer"`, title `Frontend Engineer`, description `Build accessible, responsive web interfaces, manage client-side state, and deliver dependable user experiences with product and design teams.`

| Topic / category / weight | Prompt A | Prompt B |
| --- | --- | --- |
| `ownership` / behavioral / 20 | How did you resolve a disagreement with product or design while owning a frontend feature? | What did you do when a frontend delivery you owned was at risk? |
| `component_state` / technical / 30 | What component and state boundaries would you choose for a multi-step form with shared validation? | How would you keep asynchronously loaded search results consistent when requests return out of order? |
| `web_quality` / technical / 30 | How would you make a custom dialog keyboard-accessible and usable with a screen reader? | What would you change first on a slow client-rendered page after measuring its loading and interaction performance? |
| `debugging` / behavioral / 20 | How did you isolate a frontend bug that appeared only in production? | What did you change in your delivery process after a frontend regression reached users? |

Rubric evidence: product/design trade-offs with clear personal actions; coherent state/data flow and asynchronous consistency; concrete accessibility/performance measurement and techniques; systematic debugging and regression prevention.

Allowed skill IDs: `ownership=[tradeoffs,collaboration]`, `component_state=[state_boundaries,async_consistency]`, `web_quality=[accessibility,performance]`, `debugging=[diagnosis,regression_prevention]`.

### Data analyst

`slug="data-analyst"`, title `Data Analyst`, description `Use SQL and analytical reasoning to validate data, investigate business questions, and communicate evidence-backed recommendations.`

| Topic / category / weight | Prompt A | Prompt B |
| --- | --- | --- |
| `ownership` / behavioral / 20 | How did you resolve conflicting metric definitions across teams? | What did you do when an analysis deadline conflicted with the time needed to validate the data? |
| `sql_quality` / technical / 30 | What checks would you apply before trusting a SQL join used for a revenue dashboard? | How would you handle duplicated events and missing values when calculating daily active users? |
| `analysis_reasoning` / technical / 30 | How would you determine whether a conversion-rate increase after a launch was caused by the launch? | What would you investigate when an aggregate business metric improves but key customer segments become worse? |
| `decision_impact` / behavioral / 20 | How did an analysis you delivered change a business decision? | What did you do when stakeholders interpreted your analysis more confidently than the data supported? |

Rubric evidence: explicit definitions and validation ownership; joins/grain/quality checks; sound comparison, uncertainty and causal reasoning; useful recommendations with stated limitations.

Allowed skill IDs: `ownership=[definitions,validation_ownership]`, `sql_quality=[query_grain,data_validation]`, `analysis_reasoning=[causal_reasoning,uncertainty]`, `decision_impact=[business_impact,stakeholder_communication]`.

Use shared 0–4 anchors for final rubric scores: 0 = no relevant demonstrated approach/explicit inability; 1 = major misconceptions or unsupported generic claims; 2 = workable basics with important omissions; 3 = sound role-appropriate explanation, trade-offs and validation; 4 = thorough reasoning with concrete evidence, limits and outcomes. Distinguish unassessed topics explicitly even though they add zero demonstrated points. Scores are interview feedback, not a hiring decision or a claim about protected/personal traits.

## Critical files and anchors

- `entrypoints/api/http/fastapi_/factory.py::configure_state`, `_update_sub_mount_state`, `lifespan` — mounted-app state must receive new services; do not duplicate voice supervisors here.
- `src/infra/database/sqlalchemy_.py::begin_session` (around 148) — commits on successful context exit; never keep this context open over model/media calls or share it among tasks.
- `tests/conftest.py::db_session`, `http_client` (around 54–78) — existing fixtures replace commit with flush and override dependencies on both root and mounted apps; cross-process durability needs a separate committed fixture.
- `src/infra/llm/adapter.py::generate_completion` (around 98–156) — preserve the completion port and usage logging while tightening only OpenRouter schema handling; parsed output still needs domain validation.
- `entrypoints/cli/_gunicorn_conf.py::_workers_formula` — API processes currently scale with CPU count, so voice ownership/admission must be DB-backed and the agent must have a separate command.

## Verification

### Documentation-first handoff check

Before application implementation, confirm `zFiles/interviewer-backend-plan.md` contains the complete current approved plan and `zFiles/frontend-integration-guide.md` contains the detailed self-contained guide, not only an outline or an unavailable session link. Read both files to verify the guide's relative plan link resolves and that endpoint paths, JSON names, token lifetimes, 202/409 connection behavior, terminal-state rules and evaluation envelopes agree. Preserve the existing `zFiles/` ignore rule and unrelated files. Documents must state planned versus actually exercised behavior accurately; writing them is not evidence that the service or frontend has run.


### Automated behavior checks

Prerequisites: run from repository root with `uv`, Python 3.14, and a working Docker-compatible container runtime. The existing autouse fixture starts `postgres:18` even for a selected test. Set the explicit testing JWT secret in pytest environment configuration; ordinary tests do not require real provider credentials. Keep the existing probe and reverse-URL contracts.

Add behavior-focused tests in `tests/test_auth.py`, `tests/test_interview_policy.py`, `tests/test_interviews.py`, and `tests/test_voice_interview.py`. Use deterministic model-result fixtures for domain tests, not string/implementation snapshots. Cover:

- Login returns a token that successfully calls `/auth/me` and maps JWT `sub` to the active DB user; invalid/expired/wrong-audience/missing-claim JWTs and disabled accounts fail. A second test user's token cannot read, connect to, end or evaluate the first user's interview.
- Seed twice → exactly the same three slug/ID pairs; create-request retry → one interview; same request with a different job → 409. Changing a job pack after creation leaves the interview snapshot unchanged.
- A complete, well-evidenced fixture produces exactly six answered questions with follow-ups at 2 and 4 linked to valid prior quotes. Low but assessable performance still stops at six. Ambiguous evidence extends in the documented gap order, never beyond ten; the final evaluation flags remaining gaps.
- Repeated STT fragments, duplicate completed-turn callbacks, exact spoken controls and interrupted/replayed questions do not add extra answers/questions. A sentence merely quoting `end the interview` remains answer data. Pending/applied markers distinguish a saved unassessed answer from an already handled repeat on resume.
- Simulate speech starting while a model call is blocked: the early input epoch invalidates that output before it is spoken, the following completed hook appends to the correct answer, and the same never-begun next-question ordinal is revised. Do not use a second serialized hook as the only invalidation signal.
- Reject unknown question/skill IDs, non-verbatim quotes, cross-interview or cross-topic evidence, and model-proposed cancellation/extra fields. A second invalid model result pauses without fabricated speech/evaluation. Unassessed topics and the final score are derived from saved signals, not model-supplied overall totals.
- Exercise the installed SDK's early `speech_created` ordering for both explicit `say` and generated replies: completion/transcript observations bind to the correct reply slot/question even when the handle is cancelled or empty. The last user answer is saved without `StopResponse` or `llm=None`.
- Concurrent admission yields at most two valid leases. A retry during provisioning returns 202 without a token/second dispatch, ready retry preserves original expiry, and failed/expired retries require a new UUID. No participant joining within 60 seconds releases capacity. Replacement fences old progress; already absent room close is idempotent.
- `/connect` rejects evaluating/evaluation_failed/completed/cancelled sessions, and `/end` cannot convert a natural finish into cancellation. A stale evaluation claim cannot overwrite its replacement on success or failure; completed retries return the same stored envelope without another provider call.

For actual commit visibility, add a fixture that uses two independently connected `SqlDBClientAdapter` instances with genuine commits, not the existing flush/rollback `db_session`. Create uniquely owned records and delete only those test records at teardown. Write an answer/decision through one adapter, close it, and read/resume via the other: IDs, count and question must match.

Commands after implementation:

```sh
uv run pytest tests/test_auth.py tests/test_interview_policy.py tests/test_interviews.py tests/test_voice_interview.py
uv run just cov
```

The repository's existing 90% coverage threshold remains unchanged; do not narrow coverage sources to make the new feature pass. Planning has not run or established the baseline coverage. Cover consumer-visible behavior/error paths in reused infrastructure where needed rather than adding mock-echo or source-text assertions.

### Docker and real-provider voice proof

Merge the required keys into the existing local `.env` without overwriting unrelated values. Prerequisites: a LiveKit Cloud project/URL/key/secret, OpenRouter access to the selected schema-capable model, a Deepgram account/key, an ElevenLabs account/key with access to the chosen voice/model, and provider credits. Disable LiveKit project recording/Agent observability and leave OpenRouter input/output logging and training opt-ins off. These credentials must never be pasted into the plan or committed.

From repository root:

```sh
docker compose up --build -d
docker compose run --rm api uv run python manage.py create-user --email reviewer@example.com
curl -f http://127.0.0.1:8000/api/v1/probes/health
curl -f http://127.0.0.1:8000/api/v1/probes/ready
curl -f http://127.0.0.1:8000/api/v1/jobs
```

Expected: migration/seed service completes successfully on an empty database, API and agent run without a `.git` checkout, health/ready return 200, catalogue contains the exact three roles, and reviewer creation prompts for a hidden password. Restart API and agent without resetting the database and confirm the catalogue/account persist.

Create a throwaway `/tmp/aq-interviewer-voice-smoke.py` harness during implementation (not a product frontend or a permanent repository scaffold). Its CLI is `--base-url http://127.0.0.1:8000 --role <slug> --scenario complete|resume`; read reviewer credentials via hidden prompt/environment, not password arguments. It logs in, creates an interview, handles a 202 connect response by retrying the same request UUID until ready, then joins that exact room using `livekit.rtc.Room`. Subscribe to agent audio and publish spoken PCM/WAV using `AudioSource`, `LocalAudioTrack`, paced frames, and `TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)` so the track satisfies the audio-only grant; do not copy the official WAV example's `SOURCE_UNKNOWN`. Generate canned-answer clips with ElevenLabs into memory/temporary files, never inject final text in place of STT.

For each of the three roles, run:

```sh
uv run python /tmp/aq-interviewer-voice-smoke.py --base-url http://127.0.0.1:8000 --role backend-engineer --scenario complete
uv run python /tmp/aq-interviewer-voice-smoke.py --base-url http://127.0.0.1:8000 --role frontend-engineer --scenario complete
uv run python /tmp/aq-interviewer-voice-smoke.py --base-url http://127.0.0.1:8000 --role data-analyst --scenario complete
uv run python /tmp/aq-interviewer-voice-smoke.py --base-url http://127.0.0.1:8000 --role backend-engineer --scenario resume
```

Use concrete spoken examples, not just filler: backend idempotency keys/transactional stock updates/an incident and ownership trade-off; frontend stale-request cancellation/dialog focus and keyboard behavior/a production regression; analyst join grain/deduplication/a controlled comparison and stakeholder uncertainty. A follow-up answer adds a specific constraint/outcome to the prior example. The harness waits for each new approved question and its completed delivery before publishing an answer, except the deliberate interruption case.

The harness must record observed interview/question/answer IDs and verify: subscribed agent audio actually arrives; six to ten questions are answered; at least two follow-ups quote their linked prior answer and stay within the selected role; state exposes topic evidence/gaps and code-generated reasons; the full Q/A is retrievable; final JSON has evidence-backed strengths/concerns, four rubric scores and the computed 0–100 total.

`resume` scenario: answer two questions, interrupt the next question, disconnect, reconnect using a new connect-request UUID, and verify the same unanswered question ID is replayed with the previous answers intact. Complete the interview, restart backend processes, and retrieve the same stored transcript/evaluation. Also exercise explicit early end: partial text remains, status is cancelled, no fabricated score.

The proof is local backend transport/provider behavior, not a claim that browser microphone permissions, a React UI, or public deployment are implemented. A temporary microphone client may supplement the SDK proof, but no frontend product code is part of the deliverable. No service, provider call, test, or installation has been run in plan mode.

## Assumptions & contingencies

- The user supplies valid LiveKit, OpenRouter, Deepgram and ElevenLabs credentials privately at implementation time. If a key/credit is unavailable, finish reachable backend/test/container work, report that exact missing prerequisite, and do not declare real-provider voice verification complete or substitute a mocked provider.
- The default live planner and final evaluator are both `openai/gpt-6-luna` through OpenRouter. OpenRouter advertises response-format support; environment overrides for either model must support strict JSON Schema. An incompatible override fails visibly rather than silently switching providers or weakening answer-linked validation. No measured Luna latency or one-question-stall remedy is implied.
- Default ElevenLabs voice is the current documented Python-plugin voice. If the account cannot use it, select a voice ID the user has access to via `ELEVEN_VOICE_ID`; do not switch TTS vendors or clone a voice. Keep the ElevenLabs model configurable for account availability while preserving the streaming/English contract.
- Real interview transcripts/evaluations are retained until the owner removes demo data. No application audio/video recording, automatic expiry, replay or analytics is included. Provider-side retention/settings are distinct and must not be represented as controlled solely by this application.
- The owner will provision one shared reviewer account, but data ownership remains per user and is tested with separate users. Shared-account reviewers consequently share that account's data; this is not individual-candidate privacy or a multi-tenant identity product.
- Deliver Docker-ready/local voice proof first; do not spend this phase deploying a public website or building React screens. The later frontend needs to implement login, audio connection, state/transcript/result rendering and hosting against these contracts.
