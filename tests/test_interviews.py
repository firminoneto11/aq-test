import json
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
from domain.models import (
    Interview,
    InterviewAnswer,
    InterviewDecision,
    InterviewQuestion,
    Job,
    TranscriptEntry,
    User,
)
from entrypoints.api.http.fastapi_.factory import ASGIFactory
from entrypoints.api.http.fastapi_.types_ import AsgiApp


class ScenarioLLM:
    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: None = None,
        schema_output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = json.loads(user_message)
        if "eligible_bank_ids" in payload:
            return {"bank_question_id": payload["eligible_bank_ids"][0]}
        if "questions_and_answers" in payload:
            answers = payload["questions_and_answers"]
            return {
                "strengths": [
                    {
                        "text": "Explained an API trade-off",
                        "answer_ids": [answers[2]["answer_id"]],
                    }
                ],
                "concerns": [],
                "rubric_scores": [
                    {
                        "topic_id": topic["id"],
                        "score": 3,
                        "answer_ids": [
                            answer["answer_id"]
                            for answer in answers
                            if answer["topic_id"] == topic["id"]
                        ][:1],
                    }
                    for topic in payload["topics"]
                ],
            }
        answers = payload["all_answers"]
        topic_answers = payload["topic_answers"]
        current = topic_answers[-1]
        next_question = len(answers) + 1
        role_topics = payload["role_pack"]["topics"]
        quote = current["text"]
        if next_question in (2, 4):
            bank_question_id = None
            follow_up_text = f'You mentioned "{quote}" — what changed as a result?'
            source_quote = quote
        elif next_question in (3, 5, 6):
            role_topic = {3: 1, 5: 2, 6: 3}[next_question]
            bank_question_id = role_topics[role_topic]["questions"][0]["id"]
            follow_up_text = None
            source_quote = None
        else:
            bank_question_id = None
            follow_up_text = None
            source_quote = None
        return {
            "signal": {
                "topic_id": payload["just_answered_topic"],
                "status": "assessed",
                "skill_ids": [],
                "evidence": [{"answer_id": current["answer_id"], "quote": quote}],
            },
            "bank_question_id": bank_question_id,
            "follow_up_text": follow_up_text,
            "source_quote": source_quote,
        }

    def count_tokens(self, text: str) -> int:
        return len(text)


async def remove_interview(
    db: SqlDBClientPort,
    interview_id: UUID,
    user_id: UUID,
    *,
    remove_user: bool = True,
):
    async with db.begin_session() as session:
        interview = await session.get(Interview, interview_id)
        assert interview is not None
        interview.current_question_id = None
        interview.answerable_question_id = None
        await session.flush()
        await session.execute(
            sa.delete(TranscriptEntry).where(
                TranscriptEntry.interview_id == interview_id
            )
        )
        await session.execute(
            sa.delete(InterviewDecision).where(
                InterviewDecision.interview_id == interview_id
            )
        )
        followups = (
            await session.scalars(
                sa.select(InterviewQuestion.id)
                .where(
                    InterviewQuestion.interview_id == interview_id,
                    InterviewQuestion.kind == "follow_up",
                )
                .order_by(InterviewQuestion.ordinal.desc())
            )
        ).all()
        for question_id in followups:
            # A later follow-up may cite this follow-up's answer as its source.
            await session.execute(
                sa.delete(InterviewAnswer).where(
                    InterviewAnswer.question_id == question_id
                )
            )
            await session.execute(
                sa.delete(InterviewQuestion).where(InterviewQuestion.id == question_id)
            )
        await session.execute(
            sa.delete(InterviewAnswer).where(
                InterviewAnswer.interview_id == interview_id
            )
        )
        await session.execute(
            sa.delete(InterviewQuestion).where(
                InterviewQuestion.interview_id == interview_id
            )
        )
        await session.execute(sa.delete(Interview).where(Interview.id == interview_id))
        if remove_user:
            await session.execute(sa.delete(User).where(User.id == user_id))


async def test_role_grounded_six_turns_persist_and_evaluate(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
):
    _, app = asgi_config
    await seed_jobs(db)
    async with db.begin_session() as session:
        job = await session.scalar(sa.select(Job).where(Job.slug == "backend-engineer"))
        assert job is not None
        job_id = job.id
    service = InterviewService(db, cast(LLMPort, ScenarioLLM()))
    user = await AuthUseCase(
        db=db,
        passwords=app.state.passwords,
        secret=settings.JWT_SECRET,
        algorithm="HS256",
    ).create_user(f"interview-{uuid4()}@example.com", "correct-horse-battery-staple")
    interview_id: UUID | None = None
    try:
        state, created = await service.create(user.id, job_id, uuid4())
        assert created
        interview_id = state.id
        run_id = uuid4()
        async with db.begin_session() as session:
            interview = await session.get(Interview, state.id)
            assert interview is not None
            interview.run_id = run_id
            interview.status = "active"
            interview.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)

        approved = await service.prepare_reply(state.id, run_id, lambda: True)
        answers = (
            "I aligned two teams around a clear trade-off.",
            "I measured the delivery impact after the change.",
            "I used idempotency keys for safe API retries.",
            "I monitored duplicate client requests before deployment.",
            "I used transactions to protect stock reservations.",
            "I diagnosed an incident and added prevention checks.",
        )
        for index, spoken in enumerate(answers, start=1):
            assert approved is not None and approved.kind == "question"
            assert approved.question_id is not None
            async with db.begin_session() as session:
                interview = await session.get(Interview, state.id)
                question = await session.get(InterviewQuestion, approved.question_id)
                assert interview is not None and question is not None
                interview.answerable_question_id = question.id
                question.delivery_status = "completed"
            recorded = await service.record_turn(
                state.id, run_id, f"utterance-{index}", approved.question_id, spoken
            )
            assert recorded.kind == "answer"
            repeated_callback = await service.record_turn(
                state.id, run_id, f"utterance-{index}", approved.question_id, spoken
            )
            assert repeated_callback.kind == "duplicate"
            approved = await service.prepare_reply(state.id, run_id, lambda: True)

        assert approved is not None and approved.kind == "closing"
        final_state = await service.get_state(user.id, state.id)
        assert final_state.status == "evaluating"
        assert final_state.answered_count == 6
        assert final_state.follow_up_count == 2
        transcript = await service.get_transcript(user.id, state.id)
        assert [question.ordinal for question in transcript.questions] == list(
            range(1, 7)
        )
        for followup_ordinal in (2, 4):
            followup = transcript.questions[followup_ordinal - 1]
            assert followup.kind == "follow_up"
            source = transcript.questions[followup_ordinal - 2].answer
            assert source is not None
            assert followup.source_answer_id == source.id
            assert followup.source_quote is not None
            assert followup.source_quote in followup.text

        result = await service.evaluate(state.id)
        assert result.status == "completed"
        assert result.result is not None
        assert result.result.overall_score == 75
        assert result.result.gaps == []
        assert len(result.result.rubric_scores) == 4
        assert (await service.evaluate(state.id)).result == result.result
        persisted = await service.get_transcript(user.id, state.id)
        answer_texts = []
        for question in persisted.questions:
            assert question.answer is not None
            answer_texts.append(question.answer.text)
        assert answer_texts == list(answers)
    finally:
        if interview_id is not None:
            await remove_interview(db, interview_id, user.id)
        else:
            async with db.begin_session() as session:
                await session.execute(sa.delete(User).where(User.id == user.id))


class InvalidBankLLM(ScenarioLLM):
    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: None = None,
        schema_output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return {"bank_question_id": "unlisted-question"}


async def test_invalid_question_is_not_spoken_or_counted_and_other_user_is_denied(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
):
    _, app = asgi_config
    await seed_jobs(db)
    async with db.begin_session() as session:
        job = await session.scalar(sa.select(Job).where(Job.slug == "data-analyst"))
        assert job is not None
        job_id = job.id
    auth = AuthUseCase(
        db=db,
        passwords=app.state.passwords,
        secret=settings.JWT_SECRET,
        algorithm="HS256",
    )
    owner = await auth.create_user(
        f"owner-{uuid4()}@example.com", "correct-horse-battery-staple"
    )
    stranger = await auth.create_user(
        f"stranger-{uuid4()}@example.com", "correct-horse-battery-staple"
    )
    service = InterviewService(db, cast(LLMPort, InvalidBankLLM()))
    interview_id: UUID | None = None
    try:
        state, created = await service.create(owner.id, job_id, uuid4())
        assert created
        interview_id = state.id
        with pytest.raises(GenericException) as denied:
            await service.get_state(stranger.id, state.id)
        assert denied.value.code == 404

        run_id = uuid4()
        async with db.begin_session() as session:
            interview = await session.get(Interview, state.id)
            assert interview is not None
            interview.run_id = run_id
            interview.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
        assert await service.prepare_reply(state.id, run_id, lambda: True) is None
        paused = await service.get_state(owner.id, state.id)
        assert paused.status == "paused"
        assert paused.last_error_code == "llm_invalid_output"
        assert paused.question_count == 0
        assert paused.answered_count == 0
        assert (await service.get_transcript(owner.id, state.id)).questions == []
    finally:
        if interview_id is not None:
            await remove_interview(db, interview_id, owner.id)
        else:
            async with db.begin_session() as session:
                await session.execute(sa.delete(User).where(User.id == owner.id))
        async with db.begin_session() as session:
            await session.execute(sa.delete(User).where(User.id == stranger.id))
