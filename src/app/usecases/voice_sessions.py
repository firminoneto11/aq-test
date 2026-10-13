"""Owner-scoped voice admission, idempotent grants, and fenced worker leases."""

from datetime import UTC, datetime, timedelta
from typing import Literal
from uuid import UUID, uuid4

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict

from app.exceptions import GenericException
from app.ports import SqlDBClientPort, VoiceGatewayPort
from domain.interviews import InterviewState
from domain.models.interview import Interview
from shared import logging

from .interviews import (
    build_interview_state,
    get_owned_interview,
    record_interview_decision,
)

_UNFINISHED = frozenset({"created", "active", "paused"})
_ADMISSION_LOCK = (
    0x4151494E54455256  # Shared PostgreSQL transaction lock across API processes.
)
logger = logging.get_logger()
_GRANT_SECONDS = 300
_WORKER_ERRORS = frozenset(
    {
        "llm_unavailable",
        "llm_invalid_output",
        "stt_unavailable",
        "tts_unavailable",
        "join_timeout",
        "lease_lost",
        "voice_dispatch_failed",
    }
)
_NORMAL_RELEASES = frozenset(
    {"disconnect", "idle", "completed", "cancelled", "replaced"}
)


class ConnectionPending(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interview_id: UUID
    run_id: UUID
    status: Literal["provisioning"] = "provisioning"
    retry_after_ms: Literal[500] = 500


class ConnectionGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interview_id: UUID
    run_id: UUID
    server_url: str
    room_name: str
    participant_identity: str
    participant_token: str
    expires_at: datetime
    state: InterviewState


ConnectionResponse = ConnectionGrant | ConnectionPending


class VoiceSessionService:
    def __init__(
        self,
        db: SqlDBClientPort,
        gateway: VoiceGatewayPort | None,
        *,
        server_url: str = "",
        max_active: int = 2,
        lease_seconds: int = 30,
    ):
        self._db = db
        self._gateway = gateway
        self._server_url = server_url
        self._max_active = max_active
        self._lease_seconds = lease_seconds

    async def connect(
        self, owner_id: UUID, interview_id: UUID, request_id: UUID
    ) -> ConnectionResponse:
        gateway = self._gateway
        # Authentication and terminal state always take precedence over configuration.
        async with self._db.begin_session() as session:
            interview = await get_owned_interview(session, owner_id, interview_id)
            if interview.status not in _UNFINISHED:
                raise GenericException("Interview cannot be resumed", 409)
        if gateway is None or not self._server_url:
            raise GenericException("Voice service unavailable", 503)

        old_room: str | None = None
        async with self._db.begin_session() as session:
            # The lock serializes the aggregate capacity check and reservation across
            # interviews/processes; row locking alone cannot protect this aggregate.
            await session.execute(
                sa.select(sa.func.pg_advisory_xact_lock(_ADMISSION_LOCK))
            )
            interview = await session.scalar(
                sa.select(Interview)
                .where(Interview.id == interview_id, Interview.user_id == owner_id)
                .with_for_update()
            )
            if interview is None:
                raise GenericException("Interview not found", 404)
            if interview.status not in _UNFINISHED:
                raise GenericException("Interview cannot be resumed", 409)
            now = datetime.now(UTC)
            if interview.connect_request_id == request_id:
                if (
                    interview.run_id is None
                    or interview.lease_expires_at is None
                    or interview.lease_expires_at <= now
                    or interview.connection_state == "failed"
                    or interview.grant_expires_at is None
                    or interview.grant_expires_at <= now
                ):
                    raise GenericException("Connection attempt expired", 409)
                run_id = interview.run_id
                room_name = interview.room_name
                expires_at = interview.grant_expires_at
                if interview.connection_state == "provisioning":
                    return ConnectionPending(interview_id=interview_id, run_id=run_id)
                if room_name is None or interview.grant_issued_at is None:
                    raise GenericException("Connection attempt expired", 409)
                retry = True
            else:
                active = await session.scalar(
                    sa.select(sa.func.count())
                    .select_from(Interview)
                    .where(
                        Interview.id != interview_id,
                        Interview.status.in_(_UNFINISHED),
                        Interview.lease_expires_at > now,
                    )
                )
                if active is None or active >= self._max_active:
                    raise GenericException("Voice capacity reached", 429)
                old_room = interview.room_name
                run_id = uuid4()
                room_name = f"interview-{interview_id}-{run_id}"
                expires_at = now + timedelta(seconds=self._lease_seconds)
                interview.run_id = run_id
                interview.worker_job_id = None
                interview.room_name = room_name
                interview.connect_request_id = request_id
                interview.lease_expires_at = expires_at
                interview.connection_state = "provisioning"
                interview.grant_issued_at = None
                # Until ready, this column holds the immutable provisioning deadline.
                # Worker heartbeats renew the lease but never extend this deadline.
                interview.grant_expires_at = expires_at
                interview.answerable_question_id = None
                interview.last_error_code = None
                interview.revision += 1
                retry = False

        if retry:
            return await self._grant(
                gateway, owner_id, interview_id, run_id, room_name, expires_at
            )

        try:
            if old_room is not None:
                await gateway.close_room(old_room)
            await gateway.create_room(room_name)
            await gateway.dispatch(room_name, interview_id, run_id)
            async with self._db.begin_session() as session:
                interview = await session.scalar(
                    sa.select(Interview)
                    .where(Interview.id == interview_id, Interview.user_id == owner_id)
                    .with_for_update()
                )
                now = datetime.now(UTC)
                if (
                    interview is None
                    or interview.status not in _UNFINISHED
                    or interview.run_id != run_id
                    or interview.connection_state != "provisioning"
                    or interview.lease_expires_at is None
                    or interview.lease_expires_at <= now
                    or interview.grant_expires_at is None
                    or interview.grant_expires_at <= now
                ):
                    raise GenericException("Voice service unavailable", 503)
                expires_at = now + timedelta(seconds=_GRANT_SECONDS)
                # Token signing is local/synchronous. A signer failure must not leave
                # a ready reservation for which no usable grant can be issued.
                interview.connection_state = "ready"
                interview.grant_issued_at = now
                interview.grant_expires_at = expires_at
                interview.revision += 1
                state = await build_interview_state(session, interview)
                # Signing is local: a failure rolls back readiness before leaving this transaction.
                grant = self._grant_token(
                    gateway, interview_id, run_id, room_name, expires_at, state
                )
            return grant
        except Exception as exc:
            await self._failed_attempt(interview_id, run_id, room_name, gateway)
            if isinstance(exc, GenericException) and exc.code != 503:
                raise
            raise GenericException("Voice service unavailable", 503) from exc

    async def _grant(
        self,
        gateway: VoiceGatewayPort,
        owner_id: UUID,
        interview_id: UUID,
        run_id: UUID,
        room_name: str,
        expires_at: datetime,
    ) -> ConnectionGrant:
        try:
            async with self._db.begin_session() as session:
                interview = await session.scalar(
                    sa.select(Interview)
                    .where(Interview.id == interview_id, Interview.user_id == owner_id)
                    .with_for_update()
                )
                if (
                    interview is None
                    or interview.status not in _UNFINISHED
                    or interview.run_id != run_id
                    or interview.room_name != room_name
                    or interview.connection_state != "ready"
                    or interview.lease_expires_at is None
                    or interview.lease_expires_at <= datetime.now(UTC)
                    or interview.grant_expires_at != expires_at
                    or expires_at <= datetime.now(UTC) + timedelta(seconds=1)
                ):
                    raise GenericException("Connection attempt expired", 409)
                state = await build_interview_state(session, interview)
                return self._grant_token(
                    gateway, interview_id, run_id, room_name, expires_at, state
                )
        except ValueError as exc:
            if expires_at <= datetime.now(UTC) + timedelta(seconds=1):
                raise GenericException("Connection attempt expired", 409) from exc
            raise GenericException("Voice service unavailable", 503) from exc

    def _grant_token(
        self,
        gateway: VoiceGatewayPort,
        interview_id: UUID,
        run_id: UUID,
        room_name: str,
        expires_at: datetime,
        state: InterviewState,
    ) -> ConnectionGrant:
        identity = f"candidate-{interview_id}"
        return ConnectionGrant(
            interview_id=interview_id,
            run_id=run_id,
            server_url=self._server_url,
            room_name=room_name,
            participant_identity=identity,
            participant_token=gateway.issue_token(room_name, identity, expires_at),
            expires_at=expires_at,
            state=state,
        )

    async def _failed_attempt(
        self,
        interview_id: UUID,
        run_id: UUID,
        room_name: str,
        gateway: VoiceGatewayPort,
    ) -> None:
        # Even if room deletion fails, a fenced worker loses its lease and shuts
        # down at its next heartbeat. Never undo a replacement or a natural finish.
        try:
            await gateway.close_room(room_name)
        except Exception:
            await logger.awarning(
                "voice.room.cleanup.failed", room_name=room_name, exc_info=True
            )
        async with self._db.begin_session() as session:
            interview = await session.scalar(
                sa.select(Interview)
                .where(Interview.id == interview_id)
                .with_for_update()
            )
            if interview is None or interview.run_id != run_id:
                return
            interview.lease_expires_at = None
            interview.worker_job_id = None
            interview.connection_state = "failed"
            if interview.status in _UNFINISHED:
                interview.status = "paused"
                interview.last_error_code = "voice_dispatch_failed"
            interview.revision += 1

    async def end(self, owner_id: UUID, interview_id: UUID) -> InterviewState:
        async with self._db.begin_session() as session:
            interview = await session.scalar(
                sa.select(Interview)
                .where(Interview.id == interview_id, Interview.user_id == owner_id)
                .with_for_update()
            )
            if interview is None:
                raise GenericException("Interview not found", 404)
            room_name = interview.room_name
            cancelling = interview.status in _UNFINISHED
            changed = cancelling
            if cancelling:
                interview.status = "cancelled"
                interview.finish_reason = "user_stop"
                interview.current_question_id = None
                interview.answerable_question_id = None
            if interview.run_id is not None or interview.lease_expires_at is not None:
                interview.run_id = None
                interview.worker_job_id = None
                interview.lease_expires_at = None
                interview.connection_state = "failed"
                interview.grant_expires_at = None
                changed = True
            if cancelling:
                session.add(
                    record_interview_decision(
                        interview, action="finish", code="user_stop"
                    )
                )
            elif changed:
                interview.revision += 1

        if room_name is not None and self._gateway is not None:
            try:
                await self._gateway.close_room(room_name)
            except Exception as exc:
                async with self._db.begin_session() as session:
                    interview = await session.scalar(
                        sa.select(Interview)
                        .where(
                            Interview.id == interview_id, Interview.user_id == owner_id
                        )
                        .with_for_update()
                    )
                    if (
                        interview is not None
                        and interview.room_name == room_name
                        and interview.run_id is None
                    ):
                        interview.last_error_code = "voice_dispatch_failed"
                        interview.revision += 1
                raise GenericException("Voice service unavailable", 503) from exc
            async with self._db.begin_session() as session:
                interview = await session.scalar(
                    sa.select(Interview)
                    .where(Interview.id == interview_id, Interview.user_id == owner_id)
                    .with_for_update()
                )
                if (
                    interview is not None
                    and interview.room_name == room_name
                    and interview.run_id is None
                ):
                    interview.room_name = None
        async with self._db.begin_session() as session:
            return await build_interview_state(
                session, await get_owned_interview(session, owner_id, interview_id)
            )

    async def claim_run(self, interview_id: UUID, run_id: UUID, job_id: str) -> bool:
        async with self._db.begin_session() as session:
            interview = await session.scalar(
                sa.select(Interview)
                .where(Interview.id == interview_id)
                .with_for_update()
            )
            now = datetime.now(UTC)
            if (
                not job_id
                or interview is None
                or interview.run_id != run_id
                or interview.status not in _UNFINISHED
                or interview.connection_state == "failed"
                or interview.lease_expires_at is None
                or interview.lease_expires_at <= now
                or (
                    interview.connection_state == "provisioning"
                    and (
                        interview.grant_expires_at is None
                        or interview.grant_expires_at <= now
                    )
                )
            ):
                return False
            if interview.worker_job_id is not None:
                return interview.worker_job_id == job_id
            interview.worker_job_id = job_id
            interview.revision += 1
            return True

    async def renew_lease(self, interview_id: UUID, run_id: UUID, job_id: str) -> bool:
        async with self._db.begin_session() as session:
            interview = await session.scalar(
                sa.select(Interview)
                .where(Interview.id == interview_id)
                .with_for_update()
            )
            now = datetime.now(UTC)
            if (
                interview is None
                or not job_id
                or interview.run_id != run_id
                or interview.worker_job_id != job_id
                or interview.status not in _UNFINISHED
                or interview.connection_state == "failed"
                or interview.lease_expires_at is None
                or interview.lease_expires_at <= now
                or (
                    interview.connection_state == "provisioning"
                    and (
                        interview.grant_expires_at is None
                        or interview.grant_expires_at <= now
                    )
                )
            ):
                return False
            interview.lease_expires_at = now + timedelta(seconds=self._lease_seconds)
            return True

    async def release_run(
        self, interview_id: UUID, run_id: UUID, job_id: str, reason_code: str
    ) -> None:
        if reason_code not in _WORKER_ERRORS and reason_code not in _NORMAL_RELEASES:
            raise ValueError("Unknown voice release reason")
        async with self._db.begin_session() as session:
            interview = await session.scalar(
                sa.select(Interview)
                .where(Interview.id == interview_id)
                .with_for_update()
            )
            if (
                not job_id
                or interview is None
                or interview.run_id != run_id
                or interview.worker_job_id != job_id
            ):
                return
            interview.worker_job_id = None
            interview.lease_expires_at = None
            interview.connection_state = "failed"
            if interview.status in _UNFINISHED:
                was_paused = interview.status == "paused"
                interview.status = "paused"
                if reason_code in _WORKER_ERRORS:
                    interview.last_error_code = reason_code
                elif not was_paused:
                    interview.last_error_code = None
            interview.revision += 1
