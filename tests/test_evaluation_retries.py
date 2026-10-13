"""Evaluation evidence, retry, and expired-claim fencing with committed records."""

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa

from app.exceptions import GenericException
from app.job_packs import seed_jobs
from app.ports import LLMPort, SqlDBClientPort
from app.usecases.auth import AuthUseCase
from app.usecases.interviews import InterviewService
from conf import settings
from domain.models import Interview, InterviewQuestion, Job, User
from entrypoints.api.http.fastapi_.factory import ASGIFactory
from entrypoints.api.http.fastapi_.types_ import AsgiApp
from tests.test_interviews import ScenarioLLM, remove_interview


class ProposalLLM(ScenarioLLM):
    def __init__(
        self,
        *,
        invalid: str | None = None,
        scores: tuple[int, int, int, int] = (4, 2, 2, 1),
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self.invalid = invalid
        self.scores = scores
        self.started = started
        self.release = release
        self.evaluation_calls = 0

    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: None = None,
        schema_output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if '"questions_and_answers"' not in user_message:
            return await super().generate_completion(
                system_message, user_message, tools, schema_output
            )
        self.evaluation_calls += 1
        if self.started is not None and self.evaluation_calls == 1:
            self.started.set()
            assert self.release is not None
            await self.release.wait()
        # The corrective prompt appends instructions after the JSON document.
        payload, _ = json.JSONDecoder().raw_decode(user_message)
        answers: list[dict[str, str]] = payload["questions_and_answers"]
        topics: list[dict[str, Any]] = payload["topics"]
        by_topic = {
            topic["id"]: next(answer["answer_id"] for answer in answers if answer["topic_id"] == topic["id"])
            for topic in topics
        }
        proposal: dict[str, Any] = {
            "strengths": [{"text": "Explained a concrete API trade-off", "answer_ids": [by_topic["api_design"]]}],
            "concerns": [],
            "rubric_scores": [
                {"topic_id": topic["id"], "score": score, "answer_ids": [by_topic[topic["id"]]]}
                for topic, score in zip(topics, self.scores, strict=True)
            ],
        }
        if self.invalid == "cross_topic":
            proposal["rubric_scores"][1]["answer_ids"] = [by_topic["ownership"]]
        elif self.invalid == "unknown_answer":
            proposal["strengths"][0]["answer_ids"] = [str(UUID(int=0))]
        elif self.invalid == "out_of_range":
            proposal["rubric_scores"][1]["score"] = 5
        elif self.invalid == "duplicate_topic":
            proposal["rubric_scores"][1]["topic_id"] = "ownership"
        elif self.invalid == "uncited_strength":
            proposal["strengths"][0]["answer_ids"] = []
        elif self.invalid == "empty_strength":
            proposal["strengths"][0]["text"] = "  "
        return proposal


@pytest.fixture
async def finished_interview(
    db: SqlDBClientPort, asgi_config: tuple[ASGIFactory, AsgiApp]
) -> AsyncIterator[tuple[SqlDBClientPort, UUID, UUID]]:
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
    ).create_user(f"evaluation-{uuid4()}@example.com", "correct-horse-battery-staple")
    interview_id: UUID | None = None
    try:
        service = InterviewService(db, cast(LLMPort, ScenarioLLM()))
        state, created = await service.create(user.id, job_id, uuid4())
        assert created
        interview_id = state.id
        run_id = uuid4()
        async with db.begin_session() as session:
            interview = await session.get(Interview, interview_id)
            assert interview is not None
            interview.run_id = run_id
            interview.status = "active"
            interview.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        approved = await service.prepare_reply(interview_id, run_id, lambda: True)
        for ordinal, spoken in enumerate(
            (
                "I aligned two teams around a clear trade-off.",
                "I measured the delivery impact after the change.",
                "I used idempotency keys for safe API retries.",
                "I monitored duplicate client requests before deployment.",
                "I used transactions to protect stock reservations.",
                "I diagnosed an incident and added prevention checks.",
            ),
            start=1,
        ):
            assert approved is not None and approved.kind == "question"
            assert approved.question_id is not None
            async with db.begin_session() as session:
                interview = await session.get(Interview, interview_id)
                question = await session.get(InterviewQuestion, approved.question_id)
                assert interview is not None and question is not None
                interview.answerable_question_id = question.id
                question.delivery_status = "completed"
            recorded = await service.record_turn(
                interview_id, run_id, f"evaluated-answer-{ordinal}", approved.question_id, spoken
            )
            assert recorded.kind == "answer"
            approved = await service.prepare_reply(interview_id, run_id, lambda: True)
        assert approved is not None and approved.kind == "closing"
        assert (await service.get_state(user.id, interview_id)).status == "evaluating"
        yield db, user.id, interview_id
    finally:
        if interview_id is not None:
            await remove_interview(db, interview_id, user.id)
        else:
            async with db.begin_session() as session:
                await session.execute(sa.delete(User).where(User.id == user.id))


@pytest.mark.parametrize(
    "invalid",
    [
        "cross_topic", "unknown_answer", "out_of_range",
        "duplicate_topic", "uncited_strength", "empty_strength",
    ],
)
async def test_invalid_proposal_preserves_saved_interview_and_explicit_retry_completes(
    finished_interview: tuple[SqlDBClientPort, UUID, UUID], invalid: str
) -> None:
    db, owner_id, interview_id = finished_interview
    bad_llm = ProposalLLM(invalid=invalid)
    bad = InterviewService(db, cast(LLMPort, bad_llm))
    before = await bad.get_transcript(owner_id, interview_id)
    with pytest.raises(GenericException) as raised:
        await bad.evaluate(interview_id)
    assert raised.value.code == 502
    assert bad_llm.evaluation_calls == 2  # One corrective attempt cannot launder invalid evidence.
    state = await bad.get_state(owner_id, interview_id)
    assert state.status == "evaluation_failed"
    assert state.last_error_code == "evaluation_failed"
    assert state.can_resume is False
    failed = await bad.get_evaluation(owner_id, interview_id)
    assert failed.status == "failed" and failed.can_retry and failed.result is None
    after = await bad.get_transcript(owner_id, interview_id)
    assert after.questions == before.questions
    assert after.messages == before.messages

    good_llm = ProposalLLM()
    recovered = InterviewService(db, cast(LLMPort, good_llm))
    completed = await recovered.evaluate(interview_id)
    assert completed.status == "completed" and completed.result is not None
    assert [row.score for row in completed.result.rubric_scores] == [4, 2, 2, 1]
    assert completed.result.overall_score == 55  # 20 + 15 + 15 + 5.
    assert completed.result.gaps == []
    assert not completed.result.insufficient_evidence
    assert (await recovered.get_state(owner_id, interview_id)).status == "completed"
    assert (await recovered.get_evaluation(owner_id, interview_id)) == completed
    assert (await recovered.evaluate(interview_id)) == completed
    assert good_llm.evaluation_calls == 1
    persisted = await recovered.get_transcript(owner_id, interview_id)
    assert persisted.questions == before.questions
    assert persisted.messages == before.messages


@pytest.mark.parametrize("stale_invalid", [False, True])
async def test_expired_inflight_evaluator_cannot_overwrite_newer_completed_claim(
    finished_interview: tuple[SqlDBClientPort, UUID, UUID], stale_invalid: bool
) -> None:
    db, owner_id, interview_id = finished_interview
    started, release = asyncio.Event(), asyncio.Event()
    stale_llm = ProposalLLM(
        invalid="cross_topic" if stale_invalid else None,
        scores=(1, 1, 1, 1),
        started=started,
        release=release,
    )
    stale = InterviewService(db, cast(LLMPort, stale_llm))
    winner_llm = ProposalLLM()
    winner = InterviewService(db, cast(LLMPort, winner_llm))
    in_flight = asyncio.create_task(stale.evaluate(interview_id))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        concurrent = await winner.evaluate(interview_id)
        assert concurrent.status == "running" and concurrent.result is None
        assert winner_llm.evaluation_calls == 0
        async with db.begin_session() as session:
            interview = await session.get(Interview, interview_id)
            assert interview is not None and interview.evaluation_claim_id is not None
            interview.evaluation_claim_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        completed = await winner.evaluate(interview_id)
        assert completed.status == "completed" and completed.result is not None
        assert completed.result.overall_score == 55
        release.set()
        superseded = await in_flight
        assert superseded == completed
        assert (await winner.get_evaluation(owner_id, interview_id)) == completed
        assert (await winner.get_state(owner_id, interview_id)).status == "completed"
        assert (await winner.evaluate(interview_id)) == completed
        assert winner_llm.evaluation_calls == 1
        assert stale_llm.evaluation_calls == (2 if stale_invalid else 1)
    finally:
        release.set()
        if not in_flight.done():
            await in_flight
