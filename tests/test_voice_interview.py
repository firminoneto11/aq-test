import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import jwt
import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from pydantic import SecretStr

from app.job_packs import seed_jobs
from app.ports import LLMPort, SqlDBClientPort, VoiceGatewayPort
from app.usecases.auth import AuthUseCase
from app.usecases.interviews import InterviewService
from app.usecases.voice_sessions import VoiceSessionService
from conf import settings
from domain.models import Interview, Job, User
from infra.voice import LiveKitGateway

from .test_interviews import ScenarioLLM, remove_interview

if TYPE_CHECKING:
    from entrypoints.api.http.fastapi_.factory import ASGIFactory
    from entrypoints.api.http.fastapi_.types_ import AsgiApp


class RoomGateway(VoiceGatewayPort):
    def __init__(self):
        self.rooms: set[str] = set()
        self.dispatches: list[str] = []
        self.started = asyncio.Event()
        self.proceed = asyncio.Event()

    async def create_room(self, room_name: str) -> None:
        self.rooms.add(room_name)

    async def dispatch(self, room_name: str, interview_id: UUID, run_id: UUID) -> str:
        self.dispatches.append(room_name)
        self.started.set()
        await self.proceed.wait()
        return f"dispatch-{len(self.dispatches)}"

    def issue_token(
        self, room_name: str, participant_identity: str, expires_at: datetime
    ) -> str:
        return "test-room-token"

    async def close_room(self, room_name: str) -> None:
        self.rooms.discard(room_name)


def test_real_livekit_grant_allows_microphone_but_not_video(
    monkeypatch: pytest.MonkeyPatch,
):
    key = "local-test-key"
    secret = "local-test-signing-secret-with-32-bytes"
    monkeypatch.setattr(settings, "LIVEKIT_URL", "wss://example.livekit.invalid")
    monkeypatch.setattr(settings, "LIVEKIT_API_KEY", SecretStr(key))
    monkeypatch.setattr(settings, "LIVEKIT_API_SECRET", SecretStr(secret))
    expires_at = datetime.now(UTC) + timedelta(minutes=5)
    token = LiveKitGateway().issue_token("one-interview-room", "candidate", expires_at)
    claims = jwt.decode(
        token, secret, algorithms=["HS256"], options={"verify_aud": False}
    )
    grant = claims["video"]
    assert grant["room"] == "one-interview-room"
    assert grant["roomJoin"] is True
    assert grant["canSubscribe"] is True
    assert grant["canPublishData"] is True
    assert grant["canPublishSources"] == ["microphone"]
    assert "roomAdmin" not in grant
    assert claims["exp"] <= expires_at.timestamp()


async def test_pending_connect_capacity_replacement_and_owner_enforcement(
    http_client: AsyncClient,
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
):
    config, app = asgi_config
    await seed_jobs(db)
    async with db.begin_session() as session:
        job = await session.scalar(
            sa.select(Job).where(Job.slug == "frontend-engineer")
        )
        assert job is not None
        job_id = job.id
    auth = AuthUseCase(
        db=db,
        passwords=app.state.passwords,
        secret=settings.JWT_SECRET,
        algorithm="HS256",
    )
    password = "correct-horse-battery-staple"
    owner_email = f"owner-{uuid4()}@example.com"
    stranger_email = f"stranger-{uuid4()}@example.com"
    owner = await auth.create_user(owner_email, password)
    stranger = await auth.create_user(stranger_email, password)
    db_interviews = InterviewService(db, None)
    ids: list[UUID] = []
    media = RoomGateway()
    voice = VoiceSessionService(db, media, server_url="wss://test.livekit.invalid")
    original = [(mounted, mounted.state.voice_sessions) for mounted in config.apps]
    for mounted, _ in original:
        mounted.state.voice_sessions = voice
    try:
        for _ in range(3):
            state, created = await db_interviews.create(owner.id, job_id, uuid4())
            assert created
            ids.append(state.id)
        owner_token = await http_client.post(
            "/api/v1/auth/token", data={"username": owner_email, "password": password}
        )
        stranger_token = await http_client.post(
            "/api/v1/auth/token",
            data={"username": stranger_email, "password": password},
        )
        assert owner_token.status_code == stranger_token.status_code == 200
        owner_headers = {
            "Authorization": f"Bearer {owner_token.json()['access_token']}"
        }
        stranger_headers = {
            "Authorization": f"Bearer {stranger_token.json()['access_token']}"
        }
        base = "/api/v1/interviews"
        request_id = str(uuid4())
        first = asyncio.create_task(
            http_client.post(
                f"{base}/{ids[0]}/connect",
                json={"request_id": request_id},
                headers=owner_headers,
            )
        )
        await asyncio.wait_for(media.started.wait(), timeout=5)
        pending = await http_client.post(
            f"{base}/{ids[0]}/connect",
            json={"request_id": request_id},
            headers=owner_headers,
        )
        assert pending.status_code == 202
        assert pending.json()["status"] == "provisioning"
        assert "participant_token" not in pending.json()
        media.proceed.set()
        ready = await first
        assert ready.status_code == 200
        first_grant = ready.json()
        assert first_grant["state"]["id"] == str(ids[0])
        again = await http_client.post(
            f"{base}/{ids[0]}/connect",
            json={"request_id": request_id},
            headers=owner_headers,
        )
        assert again.status_code == 200
        assert again.json()["run_id"] == first_grant["run_id"]
        assert again.json()["expires_at"] == first_grant["expires_at"]
        assert len(media.dispatches) == 1

        second_request = str(uuid4())
        second = await http_client.post(
            f"{base}/{ids[1]}/connect",
            json={"request_id": second_request},
            headers=owner_headers,
        )
        assert second.status_code == 200
        full = await http_client.post(
            f"{base}/{ids[2]}/connect",
            json={"request_id": str(uuid4())},
            headers=owner_headers,
        )
        assert full.status_code == 429
        denied = await http_client.post(
            f"{base}/{ids[1]}/connect",
            json={"request_id": str(uuid4())},
            headers=stranger_headers,
        )
        assert denied.status_code == 404

        ended = await http_client.post(f"{base}/{ids[0]}/end", headers=owner_headers)
        assert ended.status_code == 200
        assert ended.json()["status"] == "cancelled"
        assert ended.json()["decision"]["reason_code"] == "user_stop"
        assert first_grant["room_name"] not in media.rooms
        no_resume = await http_client.post(
            f"{base}/{ids[0]}/connect",
            json={"request_id": str(uuid4())},
            headers=owner_headers,
        )
        assert no_resume.status_code == 409
        admitted = await http_client.post(
            f"{base}/{ids[2]}/connect",
            json={"request_id": str(uuid4())},
            headers=owner_headers,
        )
        assert admitted.status_code == 200
        old_run = UUID(second.json()["run_id"])
        assert await voice.claim_run(ids[1], old_run, "job-one")
        assert not await voice.claim_run(ids[1], old_run, "job-two")
        assert await voice.renew_lease(ids[1], old_run, "job-one")
        await voice.release_run(ids[1], old_run, "job-one", "disconnect")
        paused = await db_interviews.get_state(owner.id, ids[1])
        assert paused.status == "paused"
        old_request = await http_client.post(
            f"{base}/{ids[1]}/connect",
            json={"request_id": second_request},
            headers=owner_headers,
        )
        assert old_request.status_code == 409
        resumed = await http_client.post(
            f"{base}/{ids[1]}/connect",
            json={"request_id": str(uuid4())},
            headers=owner_headers,
        )
        assert resumed.status_code == 200
        assert resumed.json()["run_id"] != str(old_run)
        assert not await voice.renew_lease(ids[1], old_run, "job-one")
    finally:
        media.proceed.set()
        for mounted, original_service in original:
            mounted.state.voice_sessions = original_service
        for interview_id in ids:
            await remove_interview(db, interview_id, owner.id, remove_user=False)
        async with db.begin_session() as session:
            await session.execute(
                sa.delete(User).where(User.id.in_([owner.id, stranger.id]))
            )


async def test_question_becomes_answerable_when_speaking_and_observations_dedupe(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
):
    _, app = asgi_config
    await seed_jobs(db)
    async with db.begin_session() as session:
        job = await session.scalar(sa.select(Job).where(Job.slug == "backend-engineer"))
        assert job is not None
        job_id = job.id
    user = await AuthUseCase(
        db=db,
        passwords=app.state.passwords,
        secret=settings.JWT_SECRET,
        algorithm="HS256",
    ).create_user(f"voice-{uuid4()}@example.com", "correct-horse-battery-staple")
    interview_id: UUID | None = None
    try:
        service = InterviewService(db, cast(LLMPort, ScenarioLLM()))
        state, created = await service.create(user.id, job_id, uuid4())
        assert created
        interview_id = state.id
        run_id = uuid4()
        async with db.begin_session() as session:
            interview = await session.get(Interview, state.id)
            assert interview is not None
            interview.run_id = run_id
            interview.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

        question = await service.prepare_reply(state.id, run_id, lambda: True)
        assert question is not None and question.question_id is not None
        before = await service.get_state(user.id, state.id)
        assert before.answerable_question_id is None
        premature = await service.record_turn(
            state.id,
            run_id,
            "too-early",
            question.question_id,
            "I started before audio.",
        )
        assert premature.kind == "prelude"
        assert (await service.get_state(user.id, state.id)).answered_count == 0
        assert await service.begin_question(state.id, run_id, question.question_id)
        await service.observe_assistant(
            state.id,
            run_id,
            "agent-utterance",
            question.question_id,
            "question",
            question.text,
            True,
            "tts_aligned",
        )
        await service.observe_assistant(
            state.id,
            run_id,
            "agent-utterance",
            question.question_id,
            "question",
            question.text,
            True,
            "tts_aligned",
        )
        asked = await service.get_state(user.id, state.id)
        assert asked.answerable_question_id == question.question_id
        assert asked.current_question is not None
        assert asked.current_question.delivery_status == "interrupted"
        saved = await service.record_turn(
            state.id,
            run_id,
            "candidate-answer",
            question.question_id,
            "I negotiated scope with the other team.",
        )
        assert saved.kind == "answer"
        transcript = await service.get_transcript(user.id, state.id)
        assert (
            len(
                [entry for entry in transcript.messages if entry.speaker == "assistant"]
            )
            == 1
        )
        assert transcript.questions[0].answer is not None
        assert (
            transcript.questions[0].answer.text
            == "I negotiated scope with the other team."
        )
        assert [entry.sequence for entry in transcript.messages] == [1, 2, 3]
    finally:
        if interview_id is not None:
            await remove_interview(db, interview_id, user.id)
        else:
            async with db.begin_session() as session:
                await session.execute(sa.delete(User).where(User.id == user.id))
