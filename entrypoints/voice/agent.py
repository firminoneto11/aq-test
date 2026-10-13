"""Run the dedicated LiveKit interview worker (never from the HTTP process)."""

import asyncio
import json
import sys
from time import monotonic
from uuid import UUID

import sqlalchemy as sa
from livekit import rtc
from livekit.agents import (
    AgentServer,
    AgentSession,
    AutoSubscribe,
    JobContext,
    cli,
    inference,
    room_io,
    stt,
    tts,
)
from livekit.plugins import deepgram, elevenlabs, openai, silero

from app.usecases.interviews import InterviewService
from app.usecases.voice_sessions import VoiceSessionService
from conf import settings
from domain.models.interview import Interview
from infra.database.adapter import SqlDBClientAdapter
from infra.llm.adapter import LLMAdapter
from infra.voice.gateway import LiveKitGateway
from infra.voice.interviewer import InterviewerAgent
from shared import logging

logger = logging.get_logger()
_JOIN_SECONDS = 60
_FINAL_SPEECH_SECONDS = 90
_FINISHED_STATUSES = frozenset({"evaluating", "evaluation_failed", "completed"})


def _require_worker_configuration() -> None:
    missing = [
        name
        for name, value in (
            ("LIVEKIT_URL", settings.LIVEKIT_URL),
            ("LIVEKIT_API_KEY", settings.LIVEKIT_API_KEY.get_secret_value()),
            ("LIVEKIT_API_SECRET", settings.LIVEKIT_API_SECRET.get_secret_value()),
            ("OPENROUTER_API_KEY", settings.OPENROUTER_API_KEY.get_secret_value()),
            ("DEEPGRAM_API_KEY", settings.DEEPGRAM_API_KEY.get_secret_value()),
            ("ELEVEN_API_KEY", settings.ELEVEN_API_KEY.get_secret_value()),
        )
        if not value.strip()
    ]
    if missing:
        raise RuntimeError(f"Voice worker requires configuration: {', '.join(missing)}")
    if settings.VOICE_MAX_ACTIVE < 1:
        raise RuntimeError("Voice capacity must be positive")
    if not 0 < settings.VOICE_HEARTBEAT_SECONDS < settings.VOICE_LEASE_SECONDS:
        raise RuntimeError("Voice heartbeat interval must be shorter than the lease")


def _dispatch_ids(ctx: JobContext) -> tuple[UUID, UUID] | None:
    try:
        data = json.loads(ctx.job.metadata)
        if not isinstance(data, dict) or set(data) != {"interview_id", "run_id"}:
            return None
        if not all(isinstance(data[key], str) for key in data):
            return None
        interview_id = UUID(data["interview_id"])
        run_id = UUID(data["run_id"])
        if not ctx.job.id or ctx.job.room.name != f"interview-{interview_id}-{run_id}":
            return None
    except ValueError, TypeError, AttributeError:
        return None
    return interview_id, run_id


class _VoiceJob:
    def __init__(self, ctx: JobContext, interview_id: UUID, run_id: UUID) -> None:
        self.ctx = ctx
        self.interview_id = interview_id
        self.run_id = run_id
        self.job_id = ctx.job.id
        self.room_name = ctx.job.room.name
        self.candidate_identity = f"candidate-{interview_id}"
        self.db = SqlDBClientAdapter(settings.DATABASE_URL, settings.AUTOCOMMIT)
        self.gateway = LiveKitGateway()
        self.voice = VoiceSessionService(
            self.db,
            self.gateway,
            server_url=settings.LIVEKIT_URL,
            max_active=settings.VOICE_MAX_ACTIVE,
            lease_seconds=settings.VOICE_LEASE_SECONDS,
        )
        self.controller: InterviewerAgent | None = None
        self.session: AgentSession[None] | None = None
        self.heartbeat: asyncio.Task[None] | None = None
        self.stop = asyncio.Event()
        self.reason = "disconnect"
        self.claimed = False
        self._close_lock = asyncio.Lock()
        self._closed = False

    def _stop(self, reason: str) -> None:
        if not self.stop.is_set():
            self.reason = reason
            self.stop.set()

    def _candidate_disconnected(self, participant: rtc.RemoteParticipant) -> None:
        if participant.identity == self.candidate_identity:
            self._stop("disconnect")

    def _room_disconnected(self, _: rtc.DisconnectReason) -> None:
        self._stop("disconnect")

    def _session_error(self, event: object) -> None:
        source = event.source  # type: ignore[attr-defined]
        if isinstance(source, stt.STT):
            self._stop("stt_unavailable")
        elif isinstance(source, tts.TTS):
            self._stop("tts_unavailable")

    def _session_closed(self, _: object) -> None:
        self._stop("disconnect")

    def _user_state_changed(self, event: object) -> None:
        if event.new_state == "away":  # type: ignore[attr-defined]
            self._stop("idle")

    async def _reserved_room(self) -> bool:
        async with self.db.begin_session() as session:
            row = await session.scalar(
                sa.select(Interview).where(Interview.id == self.interview_id)
            )
            return bool(
                row is not None
                and row.run_id == self.run_id
                and row.room_name == self.room_name
                and row.status in ("created", "active", "paused")
            )

    async def _run_state(self) -> tuple[UUID | None, str | None, str | None]:
        async with self.db.begin_session() as session:
            row = await session.scalar(
                sa.select(Interview).where(Interview.id == self.interview_id)
            )
            if row is None:
                return None, None, None
            return row.run_id, row.worker_job_id, row.status

    async def _after_lease_loss(self) -> None:
        if self.stop.is_set():
            return
        try:
            run_id, job_id, status = await self._run_state()
            if (
                run_id == self.run_id
                and job_id == self.job_id
                and status in _FINISHED_STATUSES
            ):
                self.reason = "completed"
                controller = self.controller
                if controller is not None:
                    deadline = monotonic() + _FINAL_SPEECH_SECONDS
                    while monotonic() < deadline and not self.stop.is_set():
                        if await controller.interview_finished():
                            break
                        await asyncio.sleep(0.25)
                    else:
                        if not self.stop.is_set():
                            logger.warning(
                                "Final interview speech did not finish before shutdown"
                            )
                self.stop.set()
                return
            if run_id != self.run_id or job_id != self.job_id:
                self._stop("replaced" if status != "cancelled" else "cancelled")
            elif status == "cancelled":
                self._stop("cancelled")
            elif status == "paused":
                self._stop("disconnect")
            else:
                self._stop("lease_lost")
        except Exception:
            logger.exception("Could not read voice run after lease loss")
            self._stop("lease_lost")

    async def _heartbeat(self) -> None:
        try:
            while not self.stop.is_set():
                try:
                    await asyncio.wait_for(
                        self.stop.wait(), timeout=settings.VOICE_HEARTBEAT_SECONDS
                    )
                    return
                except TimeoutError:
                    pass
                try:
                    current = await self.voice.renew_lease(
                        self.interview_id, self.run_id, self.job_id
                    )
                except Exception:
                    logger.exception("Voice lease renewal failed")
                    self._stop("lease_lost")
                    return
                if not current:
                    await self._after_lease_loss()
                    return
        except asyncio.CancelledError:
            return

    async def _await_candidate(self, entered_at: float) -> bool:
        async def connect_and_wait() -> None:
            await self.ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
            await self.ctx.wait_for_participant(identity=self.candidate_identity)

        join = asyncio.create_task(connect_and_wait(), name="interview_candidate_join")
        stopped = asyncio.create_task(self.stop.wait(), name="interview_join_stopped")
        try:
            done, _ = await asyncio.wait(
                (join, stopped),
                timeout=max(0, _JOIN_SECONDS - (monotonic() - entered_at)),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if stopped in done and self.stop.is_set():
                return False
            if join in done:
                try:
                    await join
                except Exception:
                    self._stop("voice_dispatch_failed")
                    raise
                return (
                    not self.stop.is_set()
                    and self.candidate_identity in self.ctx.room.remote_participants
                )
            self._stop("join_timeout")
            return False
        finally:
            for task in (join, stopped):
                if not task.done():
                    task.cancel()
            await asyncio.gather(join, stopped, return_exceptions=True)

    async def run(self, entered_at: float) -> None:
        try:
            await self.db.connect()
        except Exception:
            await self.db.disconnect()
            await self.gateway.aclose()
            raise
        self.ctx.add_shutdown_callback(self.close)
        if not await self._reserved_room():
            return
        self.claimed = await self.voice.claim_run(
            self.interview_id, self.run_id, self.job_id
        )
        if not self.claimed:
            return
        self.heartbeat = asyncio.create_task(
            self._heartbeat(), name="interview_voice_lease"
        )
        self.ctx.room.on("participant_disconnected", self._candidate_disconnected)
        self.ctx.room.on("disconnected", self._room_disconnected)
        if not await self._await_candidate(entered_at):
            return
        if not await self.voice.renew_lease(
            self.interview_id, self.run_id, self.job_id
        ):
            await self._after_lease_loss()
            return
        if self.stop.is_set():
            return

        # Provider objects, including the VAD model, are allocated only after admission
        # and the intended candidate's real room join, never in download-files/import.
        try:
            provider_options = {"require_parameters": True, "data_collection": "deny"}
            planning_model = LLMAdapter(
                provider="openrouter",
                model=settings.INTERVIEW_PLANNER_MODEL,
                openrouter_provider=provider_options,
            )
            evaluation_model = (
                planning_model
                if settings.INTERVIEW_PLANNER_MODEL == settings.INTERVIEW_LLM_MODEL
                else LLMAdapter(
                    provider="openrouter",
                    model=settings.INTERVIEW_LLM_MODEL,
                    openrouter_provider=provider_options,
                )
            )
            interviews = InterviewService(
                self.db, planning_model, evaluation_llm=evaluation_model
            )
            self.controller = InterviewerAgent(
                self.interview_id,
                self.run_id,
                self.job_id,
                interviews,
                self.voice,
            )
        except Exception:
            self._stop("llm_unavailable")
            raise
        try:
            speech_to_text = deepgram.STT(
                model=settings.DEEPGRAM_MODEL,
                language="en",
                api_key=settings.DEEPGRAM_API_KEY.get_secret_value(),
            )
            vad = silero.VAD.load()
        except Exception:
            self._stop("stt_unavailable")
            raise
        try:
            text_to_speech = elevenlabs.TTS(
                model=settings.ELEVEN_MODEL,
                voice_id=settings.ELEVEN_VOICE_ID,
                api_key=settings.ELEVEN_API_KEY.get_secret_value(),
            )
        except Exception:
            self._stop("tts_unavailable")
            raise
        try:
            planner = openai.LLM.with_openrouter(
                model=settings.INTERVIEW_PLANNER_MODEL,
                api_key=settings.OPENROUTER_API_KEY.get_secret_value(),
                provider={"require_parameters": True, "data_collection": "deny"},
            )
        except Exception:
            self._stop("llm_unavailable")
            raise
        try:
            session = AgentSession(
                stt=speech_to_text,
                llm=planner,
                tts=text_to_speech,
                vad=vad,
                turn_handling={
                    "turn_detection": inference.TurnDetector(version="v1-mini"),
                    "preemptive_generation": {"enabled": False},
                    "interruption": {"enabled": True, "mode": "vad"},
                },
                use_tts_aligned_transcript=True,
                user_away_timeout=120,
            )
        except Exception:
            self._stop("voice_dispatch_failed")
            raise
        self.session = session
        session.on("error", self._session_error)
        session.on("close", self._session_closed)
        session.on("user_state_changed", self._user_state_changed)
        self.controller.bind_session(session)
        if not self.stop.is_set() and await self.voice.renew_lease(
            self.interview_id, self.run_id, self.job_id
        ):
            await session.start(
                agent=self.controller,
                room=self.ctx.room,
                room_options=room_io.RoomOptions(
                    participant_identity=self.candidate_identity,
                    text_input=False,
                    video_input=False,
                ),
                record=False,
            )
            if not self.stop.is_set():
                await self.controller.start_question()
            await self.stop.wait()
        else:
            await self._after_lease_loss()

    async def close(self, _: str = "") -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self.stop.set()
            if self.heartbeat is not None:
                self.heartbeat.cancel()
                await asyncio.gather(self.heartbeat, return_exceptions=True)
            try:
                if self.session is not None:
                    await self.session.aclose()
            except Exception:
                logger.exception("Could not close voice session")
            try:
                if self.controller is not None:
                    await self.controller.close()
            except Exception:
                logger.exception("Could not drain interview controller")
            if self.claimed:
                try:
                    await asyncio.wait_for(
                        self.gateway.close_room(self.room_name), timeout=8
                    )
                except Exception:
                    logger.exception("Could not close interview room")
                    self.reason = "voice_dispatch_failed"
                try:
                    await self.voice.release_run(
                        self.interview_id,
                        self.run_id,
                        self.job_id,
                        self.reason,
                    )
                except Exception:
                    logger.exception("Could not release interview run")
            try:
                await self.gateway.aclose()
            finally:
                await self.db.disconnect()


def _worker_load(server: AgentServer) -> float:
    return min(len(server.active_jobs) / max(settings.VOICE_MAX_ACTIVE, 1), 1.0)


server = AgentServer(
    ws_url=settings.LIVEKIT_URL or None,
    api_key=settings.LIVEKIT_API_KEY.get_secret_value() or None,
    api_secret=settings.LIVEKIT_API_SECRET.get_secret_value() or None,
    load_fnc=_worker_load,
    load_threshold=1.0,
    num_idle_processes=1,
    shutdown_process_timeout=120,
    session_end_timeout=120,
)


@server.rtc_session(agent_name="interviewer")
async def interview_job(ctx: JobContext) -> None:
    entered_at = monotonic()
    ids = _dispatch_ids(ctx)
    if ids is None:
        logger.warning("Rejecting voice job with invalid dispatch metadata or room")
        ctx.shutdown("invalid interview dispatch")
        return
    job = _VoiceJob(ctx, *ids)
    try:
        await job.run(entered_at)
    except Exception:
        logger.exception("Interview voice job failed")
    finally:
        ctx.shutdown("interview voice job finished")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ("dev", "start"):
        _require_worker_configuration()
    cli.run_app(server)
