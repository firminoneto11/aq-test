"""Consumer-visible interview recovery and voice-run fencing."""

import json
from datetime import UTC, datetime, timedelta, tzinfo
from typing import TYPE_CHECKING, Any, ClassVar, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from httpx import AsyncClient

from app.exceptions import GenericException
from app.job_packs import seed_jobs
from app.ports import LLMPort, SqlDBClientPort
from app.usecases.auth import AuthUseCase
from app.usecases.interviews import InterviewService
from app.usecases.voice_sessions import ConnectionGrant, VoiceSessionService
from conf import settings
from domain.models import Interview, Job, User
from domain.schemas import UserSchema

from .test_interviews import ScenarioLLM, remove_interview
from .test_voice_interview import RoomGateway

if TYPE_CHECKING:
    from entrypoints.api.http.fastapi_.factory import ASGIFactory
    from entrypoints.api.http.fastapi_.types_ import AsgiApp


async def _job_id(db: SqlDBClientPort) -> UUID:
    await seed_jobs(db)
    async with db.begin_session() as session:
        job = await session.scalar(sa.select(Job).where(Job.slug == "backend-engineer"))
        assert job is not None
        return job.id


async def _owner(db: SqlDBClientPort, app: AsgiApp) -> UserSchema:
    return await AuthUseCase(
        db=db,
        passwords=app.state.passwords,
        secret=settings.JWT_SECRET,
        algorithm="HS256",
    ).create_user(f"failure-{uuid4()}@example.com", "correct-horse-battery-staple")


async def _remove_rows(
    db: SqlDBClientPort, owner_ids: list[UUID], interview_ids: list[tuple[UUID, UUID]]
) -> None:
    for interview_id, owner_id in interview_ids:
        await remove_interview(db, interview_id, owner_id, remove_user=False)
    if owner_ids:
        async with db.begin_session() as session:
            await session.execute(sa.delete(User).where(User.id.in_(owner_ids)))


class FailingDispatchGateway(RoomGateway):
    def __init__(self) -> None:
        super().__init__()
        self.proceed.set()
        self.fail_next = True
        self.tokens_issued = 0

    async def dispatch(self, room_name: str, interview_id: UUID, run_id: UUID) -> str:
        job = await super().dispatch(room_name, interview_id, run_id)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("media dispatch failed")
        return job

    def issue_token(
        self, room_name: str, participant_identity: str, expires_at: datetime
    ) -> str:
        self.tokens_issued += 1
        return super().issue_token(room_name, participant_identity, expires_at)


class Clock(datetime):
    current: ClassVar[datetime] = datetime.now(UTC)

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> datetime:
        if tz is None:
            return cls.current.replace(tzinfo=None)
        return cls.current.astimezone(tz)


async def test_failed_dispatch_releases_capacity_and_fresh_attempt_issues_only_real_grant(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
) -> None:
    _, app = asgi_config
    job_id = await _job_id(db)
    owner = await _owner(db, app)
    interview_ids: list[tuple[UUID, UUID]] = []
    gateway = FailingDispatchGateway()
    voice = VoiceSessionService(
        db, gateway, server_url="wss://test.livekit.invalid", max_active=1
    )
    service = InterviewService(db, None)
    try:
        first, _ = await service.create(owner.id, job_id, uuid4())
        interview_ids.append((first.id, owner.id))
        other, _ = await service.create(owner.id, job_id, uuid4())
        interview_ids.append((other.id, owner.id))
        failed_request = uuid4()
        with pytest.raises(GenericException) as failure:
            await voice.connect(owner.id, first.id, failed_request)
        assert failure.value.code == 503
        assert failure.value.detail == "Voice service unavailable"
        assert gateway.tokens_issued == 0
        assert gateway.rooms == set()
        failed = await service.get_state(owner.id, first.id)
        assert failed.status == "paused"
        assert failed.last_error_code == "voice_dispatch_failed"
        assert failed.can_resume
        async with db.begin_session() as session:
            record = await session.get(Interview, first.id)
            assert record is not None
            assert record.lease_expires_at is None
            failed_run = record.run_id
            assert failed_run is not None
            failed_room = record.room_name
        with pytest.raises(GenericException) as expired:
            await voice.connect(owner.id, first.id, failed_request)
        assert expired.value.code == 409
        assert expired.value.detail == "Connection attempt expired"
        assert len(gateway.dispatches) == 1

        # A failed attempt holds no slot. Another interview can be admitted immediately.
        other_grant = await voice.connect(owner.id, other.id, uuid4())
        assert isinstance(other_grant, ConnectionGrant)
        assert other_grant.participant_token
        assert gateway.tokens_issued == 1
        with pytest.raises(GenericException) as full:
            await voice.connect(owner.id, first.id, uuid4())
        assert full.value.code == 429
        await voice.end(owner.id, other.id)

        resumed = await voice.connect(owner.id, first.id, uuid4())
        assert isinstance(resumed, ConnectionGrant)
        assert resumed.run_id != failed_run
        assert resumed.room_name != failed_room
        assert resumed.state.status == "paused"
        assert resumed.state.last_error_code is None
        assert gateway.tokens_issued == 2
        assert len(gateway.dispatches) == 3
    finally:
        await _remove_rows(db, [owner.id], interview_ids)


async def test_no_show_lease_expiry_frees_admission_and_cannot_be_revived(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app = asgi_config
    job_id = await _job_id(db)
    owner = await _owner(db, app)
    interview_ids: list[tuple[UUID, UUID]] = []
    gateway = RoomGateway()
    gateway.proceed.set()
    voice = VoiceSessionService(
        db, gateway, server_url="wss://test.livekit.invalid", max_active=1
    )
    service = InterviewService(db, None)
    Clock.current = datetime.now(UTC)
    monkeypatch.setattr("app.usecases.voice_sessions.datetime", Clock)
    try:
        first, _ = await service.create(owner.id, job_id, uuid4())
        interview_ids.append((first.id, owner.id))
        other, _ = await service.create(owner.id, job_id, uuid4())
        interview_ids.append((other.id, owner.id))
        request_id = uuid4()
        first_grant = await voice.connect(owner.id, first.id, request_id)
        assert isinstance(first_grant, ConnectionGrant)
        with pytest.raises(GenericException) as full:
            await voice.connect(owner.id, other.id, uuid4())
        assert full.value.code == 429

        # There is no candidate/job to renew the reservation: no sleep or scheduler race.
        Clock.current += timedelta(seconds=31)
        assert not await voice.claim_run(first.id, first_grant.run_id, "late-worker")
        assert not await voice.renew_lease(first.id, first_grant.run_id, "late-worker")
        with pytest.raises(GenericException) as expired:
            await voice.connect(owner.id, first.id, request_id)
        assert expired.value.code == 409
        assert expired.value.detail == "Connection attempt expired"
        second_grant = await voice.connect(owner.id, other.id, uuid4())
        assert isinstance(second_grant, ConnectionGrant)
        assert second_grant.run_id != first_grant.run_id
        assert len(gateway.dispatches) == 2
    finally:
        await _remove_rows(db, [owner.id], interview_ids)


async def test_old_worker_callbacks_and_heartbeat_cannot_change_replacement(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
) -> None:
    _, app = asgi_config
    job_id = await _job_id(db)
    owner = await _owner(db, app)
    interview_ids: list[tuple[UUID, UUID]] = []
    gateway = RoomGateway()
    gateway.proceed.set()
    voice = VoiceSessionService(
        db, gateway, server_url="wss://test.livekit.invalid", lease_seconds=600
    )
    service = InterviewService(db, cast(LLMPort, ScenarioLLM()))
    try:
        created, _ = await service.create(owner.id, job_id, uuid4())
        interview_ids.append((created.id, owner.id))
        old = await voice.connect(owner.id, created.id, uuid4())
        assert isinstance(old, ConnectionGrant)
        assert await voice.claim_run(created.id, old.run_id, "old-job")
        approved = await service.prepare_reply(created.id, old.run_id, lambda: True)
        assert approved is not None and approved.question_id is not None
        question_id = approved.question_id
        assert await service.begin_question(created.id, old.run_id, question_id)
        replacement = await voice.connect(owner.id, created.id, uuid4())
        assert isinstance(replacement, ConnectionGrant)
        assert replacement.run_id != old.run_id
        assert old.room_name not in gateway.rooms
        assert not await voice.claim_run(created.id, old.run_id, "old-job")
        assert not await voice.renew_lease(created.id, old.run_id, "old-job")
        assert not await service.begin_question(created.id, old.run_id, question_id)
        with pytest.raises(GenericException) as stale:
            await service.record_turn(
                created.id, old.run_id, "late-candidate", question_id, "Late answer"
            )
        assert stale.value.code == 409
        assert await voice.claim_run(created.id, replacement.run_id, "new-job")
        assert await service.begin_question(created.id, replacement.run_id, question_id)
        await service.observe_assistant(
            created.id,
            replacement.run_id,
            "new-speech",
            question_id,
            "question",
            approved.text,
            False,
            "tts_aligned",
        )
        before = await service.get_state(owner.id, created.id)
        assert before.current_question is not None
        assert before.current_question.delivery_status == "completed"
        await service.observe_assistant(
            created.id,
            old.run_id,
            "late-speech",
            question_id,
            "question",
            "What did you do when delivery was at risk?",
            True,
            "sdk_estimate",
        )
        await voice.release_run(created.id, old.run_id, "old-job", "stt_unavailable")
        after = await service.get_state(owner.id, created.id)
        assert after.revision == before.revision
        assert after.current_question == before.current_question
        assert after.answerable_question_id == question_id
        assert after.last_error_code is None
        new_run_state = await service.voice_state(created.id, replacement.run_id)
        assert new_run_state is not None and new_run_state.run_current
        transcript = await service.get_transcript(owner.id, created.id)
        assert [message.run_id for message in transcript.messages] == [
            replacement.run_id,
            old.run_id,
        ]
        assert transcript.messages[1].interrupted
        accepted = await service.record_turn(
            created.id,
            replacement.run_id,
            "new-answer",
            question_id,
            "I negotiated the scope with another team.",
        )
        assert accepted.kind == "answer"
        assert (await service.get_state(owner.id, created.id)).answered_count == 1
    finally:
        await _remove_rows(db, [owner.id], interview_ids)


class InvalidProposalLLM(ScenarioLLM):
    def __init__(self, invalid_step: str) -> None:
        self.invalid_step = invalid_step
        self.invalid_calls = 0

    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: None = None,
        schema_output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = json.loads(user_message.split("\nPrevious output was invalid:", 1)[0])
        if "all_answers" in payload:
            next_question = len(payload["all_answers"]) + 1
            if (self.invalid_step == "follow_up" and next_question == 2) or (
                self.invalid_step == "bank" and next_question == 3
            ):
                self.invalid_calls += 1
                valid = await super().generate_completion(
                    system_message,
                    user_message.split("\nPrevious output was invalid:", 1)[0],
                    tools,
                    schema_output,
                )
                if self.invalid_step == "follow_up":
                    valid["source_quote"] = "a detail the candidate never said"
                else:
                    valid["bank_question_id"] = "backend-engineer.api_design.unlisted"
                return valid
        return await super().generate_completion(
            system_message,
            user_message.split("\nPrevious output was invalid:", 1)[0],
            tools,
            schema_output,
        )


@pytest.mark.parametrize("invalid_step", ["follow_up", "bank"])
async def test_invalid_proposal_keeps_saved_answer_pending_until_fresh_run_recovers(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
    invalid_step: str,
) -> None:
    _, app = asgi_config
    job_id = await _job_id(db)
    owner = await _owner(db, app)
    interview_ids: list[tuple[UUID, UUID]] = []
    gateway = RoomGateway()
    gateway.proceed.set()
    voice = VoiceSessionService(
        db, gateway, server_url="wss://test.livekit.invalid", lease_seconds=600
    )
    malformed = InvalidProposalLLM(invalid_step)
    service = InterviewService(db, cast(LLMPort, malformed))
    try:
        created, _ = await service.create(owner.id, job_id, uuid4())
        interview_ids.append((created.id, owner.id))
        old = await voice.connect(owner.id, created.id, uuid4())
        assert isinstance(old, ConnectionGrant)
        approved = await service.prepare_reply(created.id, old.run_id, lambda: True)
        expected_answers = 1 if invalid_step == "follow_up" else 2
        for index in range(expected_answers):
            assert approved is not None and approved.question_id is not None
            assert await service.begin_question(
                created.id, old.run_id, approved.question_id
            )
            recorded = await service.record_turn(
                created.id,
                old.run_id,
                f"answer-{index}",
                approved.question_id,
                (
                    "I negotiated delivery with the other team."
                    if index == 0
                    else "I measured the outcome of that trade-off."
                ),
            )
            assert recorded.kind == "answer"
            approved = await service.prepare_reply(created.id, old.run_id, lambda: True)
        assert approved is None
        assert (
            malformed.invalid_calls == 2
        )  # One corrective attempt, then visible failure.
        paused = await service.get_state(owner.id, created.id)
        assert paused.status == "paused"
        assert paused.last_error_code == "llm_invalid_output"
        assert paused.question_count == expected_answers
        assert paused.answered_count == expected_answers
        assert paused.current_question is not None
        assert paused.current_question.ordinal == expected_answers
        transcript = await service.get_transcript(owner.id, created.id)
        assert len(transcript.questions) == expected_answers
        assert [
            q.answer.text for q in transcript.questions if q.answer is not None
        ] == [
            "I negotiated delivery with the other team.",
            *(
                ["I measured the outcome of that trade-off."]
                if expected_answers == 2
                else []
            ),
        ]
        assert [entry.processing_status for entry in transcript.messages] == [
            *(["applied"] if expected_answers == 2 else []),
            "pending",
        ]
        assert paused.rubric[0].status == (
            "assessed" if expected_answers == 2 else "unobserved"
        )
        # A new request fences the failed run and resumes from the saved pending input.
        recovered = await voice.connect(owner.id, created.id, uuid4())
        assert isinstance(recovered, ConnectionGrant)
        assert recovered.run_id != old.run_id
        assert recovered.state.last_error_code is None
        restored = InterviewService(db, cast(LLMPort, ScenarioLLM()))
        next_question = await restored.prepare_reply(
            created.id, recovered.run_id, lambda: True
        )
        assert next_question is not None and next_question.kind == "question"
        assert next_question.question_id is not None
        state = await restored.get_state(owner.id, created.id)
        assert state.status == "active"
        assert state.last_error_code is None
        assert state.answered_count == expected_answers
        assert state.question_count == expected_answers + 1
        assert state.current_question is not None
        assert state.current_question.id == next_question.question_id
        assert state.current_question.ordinal == expected_answers + 1
        assert state.current_question.kind == (
            "follow_up" if expected_answers == 1 else "bank"
        )
        assert [
            entry.processing_status
            for entry in (await restored.get_transcript(owner.id, created.id)).messages
        ] == ["applied" for _ in range(expected_answers)]
    finally:
        await _remove_rows(db, [owner.id], interview_ids)


async def test_jwt_ownership_denies_every_private_route_and_end_preserves_natural_finish(
    http_client: AsyncClient,
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
) -> None:
    config, app = asgi_config
    job_id = await _job_id(db)
    owner = await _owner(db, app)
    stranger = await _owner(db, app)
    interview_ids: list[tuple[UUID, UUID]] = []
    gateway = RoomGateway()
    gateway.proceed.set()
    voice = VoiceSessionService(
        db, gateway, server_url="wss://test.livekit.invalid", lease_seconds=600
    )
    old_voice = [(mounted, mounted.state.voice_sessions) for mounted in config.apps]
    for mounted, _ in old_voice:
        mounted.state.voice_sessions = voice
    service = InterviewService(db, cast(LLMPort, ScenarioLLM()))
    try:
        created, _ = await service.create(owner.id, job_id, uuid4())
        interview_ids.append((created.id, owner.id))
        token_responses = [
            await http_client.post(
                "/api/v1/auth/token",
                data={
                    "username": user.email,
                    "password": "correct-horse-battery-staple",
                },
            )
            for user in (owner, stranger)
        ]
        assert all(response.status_code == 200 for response in token_responses)
        owner_headers, stranger_headers = [
            {"Authorization": f"Bearer {response.json()['access_token']}"}
            for response in token_responses
        ]
        base = f"/api/v1/interviews/{created.id}"
        for method, suffix, body in (
            ("GET", "", None),
            ("GET", "/transcript", None),
            ("GET", "/evaluation", None),
            ("POST", "/connect", {"request_id": str(uuid4())}),
            ("POST", "/end", None),
            ("POST", "/evaluation", None),
        ):
            response = await http_client.request(
                method, base + suffix, json=body, headers=stranger_headers
            )
            assert response.status_code == 404, (method, suffix, response.text)
        still_created = await service.get_state(owner.id, created.id)
        assert still_created.status == "created"
        assert still_created.question_count == 0
        assert still_created.answered_count == 0
        assert gateway.dispatches == []
        connected = await voice.connect(owner.id, created.id, uuid4())
        assert isinstance(connected, ConnectionGrant)
        approved = await service.prepare_reply(
            created.id, connected.run_id, lambda: True
        )
        for index, answer in enumerate(
            (
                "I aligned two teams around a clear trade-off.",
                "I measured the delivery impact after the change.",
                "I used idempotency keys for safe API retries.",
                "I monitored duplicate client requests before deployment.",
                "I used transactions to protect stock reservations.",
                "I diagnosed an incident and added prevention checks.",
            )
        ):
            assert approved is not None and approved.question_id is not None
            assert await service.begin_question(
                created.id, connected.run_id, approved.question_id
            )
            recorded = await service.record_turn(
                created.id,
                connected.run_id,
                f"answer-{index}",
                approved.question_id,
                answer,
            )
            assert recorded.kind == "answer"
            approved = await service.prepare_reply(
                created.id, connected.run_id, lambda: True
            )
        assert approved is not None and approved.kind == "closing"
        naturally_finished = await service.get_state(owner.id, created.id)
        assert naturally_finished.status == "evaluating"
        assert naturally_finished.answered_count == 6
        assert not naturally_finished.can_resume
        ended = await http_client.post(base + "/end", headers=owner_headers)
        assert ended.status_code == 200
        assert ended.json()["status"] == "evaluating"
        assert ended.json()["answered_count"] == 6
        assert ended.json()["decision"]["reason_code"] == "coverage_complete"
        assert connected.room_name not in gateway.rooms
        assert (
            await service.get_transcript(owner.id, created.id)
        ).status == "evaluating"
        no_resume = await http_client.post(
            base + "/connect", json={"request_id": str(uuid4())}, headers=owner_headers
        )
        assert no_resume.status_code == 409
        assert no_resume.json()["detail"] == "Interview cannot be resumed"
    finally:
        for mounted, original in old_voice:
            mounted.state.voice_sessions = original
        await _remove_rows(db, [owner.id, stranger.id], interview_ids)
