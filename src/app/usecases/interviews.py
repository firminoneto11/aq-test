"""Durable interview state, input capture, and policy-gated question planning."""

import asyncio
import json
import re
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Callable, Literal, cast
from uuid import UUID

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import GenericException
from app.ports import LLMPort, SqlDBClientPort
from conf import settings
from domain.interviews import (
    AnsweredQuestion,
    BankChoice,
    CurrentQuestion,
    DecisionSummary,
    Evaluation,
    EvaluationResponse,
    InterviewPolicy,
    InterviewSnapshot,
    InterviewState,
    JobSummary,
    NextStep,
    RolePack,
    RubricSignal,
    SkillDetected,
    TranscriptAnswer,
    TranscriptMessage,
    TranscriptQuestion,
    TranscriptResponse,
    TurnPlan,
    classify_control,
)
from domain.models.interview import (
    Interview,
    InterviewAnswer,
    InterviewDecision,
    InterviewQuestion,
    Job,
    TranscriptEntry,
)

from .interview_evaluation import EvaluationService

CLOSING_TEXT = (
    "Thank you. The interview is complete. Your evaluation will be available shortly."
)
_LLM_SECONDS = settings.LLM_TIMEOUT_SECONDS
_UNFINISHED = frozenset({"created", "active", "paused"})


class _GapFollowUp(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_quote: str
    follow_up_text: str


@dataclass(frozen=True, slots=True)
class RecordedTurn:
    entry_id: UUID
    captured_question_id: UUID | None
    kind: Literal["answer", "repeat", "stop", "prelude", "duplicate"]
    revision: int


@dataclass(frozen=True, slots=True)
class ApprovedReply:
    question_id: UUID | None
    kind: Literal["question", "control", "closing"]
    text: str


@dataclass(frozen=True, slots=True)
class VoiceRunState:
    status: str
    last_error_code: str | None
    revision: int
    current_question_id: UUID | None
    answerable_question_id: UUID | None
    run_current: bool


def _pack(interview: Interview) -> RolePack:
    return RolePack.model_validate(interview.job_snapshot["pack"])


def _signals(interview: Interview) -> tuple[RubricSignal, ...]:
    return tuple(RubricSignal.model_validate(item) for item in interview.signals)


def _evaluation_status(
    interview: Interview,
) -> Literal["not_ready", "running", "failed", "completed"]:
    if interview.status == "completed":
        return "completed"
    if interview.status == "evaluation_failed":
        return "failed"
    if interview.status == "evaluating":
        if (
            interview.evaluation_claim_expires_at is None
            or interview.evaluation_claim_expires_at <= datetime.now(UTC)
        ):
            return "failed"
        return "running"
    return "not_ready"


def _run_valid(interview: Interview, run_id: UUID) -> bool:
    return (
        interview.run_id == run_id
        and interview.lease_expires_at is not None
        and interview.lease_expires_at > datetime.now(UTC)
    )


async def get_owned_interview(
    session: AsyncSession, owner_id: UUID, interview_id: UUID
) -> Interview:
    interview = await session.scalar(
        sa.select(Interview).where(
            Interview.id == interview_id, Interview.user_id == owner_id
        )
    )
    if interview is None:
        raise GenericException("Interview not found", 404)
    return interview


async def _questions(
    session: AsyncSession, interview_id: UUID
) -> list[InterviewQuestion]:
    return list(
        (
            await session.scalars(
                sa.select(InterviewQuestion)
                .where(InterviewQuestion.interview_id == interview_id)
                .order_by(InterviewQuestion.ordinal)
            )
        ).all()
    )


async def _answers(session: AsyncSession, interview_id: UUID) -> list[InterviewAnswer]:
    return list(
        (
            await session.scalars(
                sa.select(InterviewAnswer).where(
                    InterviewAnswer.interview_id == interview_id
                )
            )
        ).all()
    )


async def _snapshot(
    session: AsyncSession, interview: Interview
) -> tuple[InterviewSnapshot, list[InterviewQuestion], list[InterviewAnswer]]:
    questions = await _questions(session, interview.id)
    answers = await _answers(session, interview.id)
    by_question = {question.id: question for question in questions}
    ordered = sorted(
        answers, key=lambda answer: by_question[answer.question_id].ordinal
    )
    answered_ids = {answer.question_id for answer in answers}
    snapshot = InterviewSnapshot(
        pack=_pack(interview),
        answers=tuple(
            AnsweredQuestion(
                question_id=answer.question_id,
                answer_id=answer.id,
                topic_id=by_question[answer.question_id].topic_id,
                kind=by_question[answer.question_id].kind,
                answer_text=answer.text,
                bank_question_id=by_question[answer.question_id].bank_question_id,
            )
            for answer in ordered
        ),
        signals=_signals(interview),
        used_bank_ids=frozenset(
            q.bank_question_id
            for q in questions
            if q.bank_question_id is not None
            and (
                q.delivery_status != "pending"
                or q.id in answered_ids
                or q.id == interview.answerable_question_id
            )
        ),
    )
    return snapshot, questions, answers


def _reason(code: str, topic: str | None, source: UUID | None) -> str:
    match code:
        case "initial_topic":
            return f"Opening with the required ownership topic ({topic})."
        case "required_follow_up":
            return f"Required answer-linked follow-up for topic {topic}, from answer {source}."
        case "required_topic":
            return f"Required role-pack topic {topic} in the six-question schedule."
        case "evidence_gap":
            return f"Probing an insufficient-evidence gap in topic {topic}, from answer {source}."
        case "repeat_requested":
            return "Replaying the last begun question at the candidate's request."
        case "coverage_complete":
            return "The required six questions are answered and all four topics have assessable evidence."
        case "question_limit":
            return "The ten-question limit was reached; remaining evidence gaps are reported."
        case "provider_unavailable":
            return "Question planning could not safely complete; accepted input remains saved for recovery."
        case "user_stop":
            return "The candidate explicitly requested that the interview end."
        case _:
            raise ValueError("Unrecognized interview decision")


def record_interview_decision(
    interview: Interview,
    *,
    action: Literal["ask", "finish", "pause"],
    code: str,
    question: InterviewQuestion | None = None,
    topic: str | None = None,
    source: UUID | None = None,
) -> InterviewDecision:
    interview.revision += 1
    topic = question.topic_id if question is not None else topic
    source = question.source_answer_id if question is not None else source
    return InterviewDecision(
        interview_id=interview.id,
        revision=interview.revision,
        action=action,
        question_id=question.id if question is not None else None,
        topic_id=topic,
        source_answer_id=source,
        reason_code=code,
        reason=_reason(code, topic, source),
        signals=interview.signals,
    )


async def build_interview_state(
    session: AsyncSession, interview: Interview
) -> InterviewState:
    questions = await _questions(session, interview.id)
    answers = await _answers(session, interview.id)
    current = next(
        (q for q in questions if q.id == interview.current_question_id), None
    )
    latest = await session.scalar(
        sa.select(InterviewDecision)
        .where(InterviewDecision.interview_id == interview.id)
        .order_by(InterviewDecision.revision.desc())
        .limit(1)
    )
    signals = _signals(interview)
    pack = _pack(interview)
    job_data = interview.job_snapshot["job"]
    if not isinstance(job_data, dict):
        raise ValueError("Invalid immutable job snapshot")
    covered = [signal.topic_id for signal in signals if signal.status == "assessed"]
    return InterviewState(
        id=interview.id,
        job=JobSummary.model_validate(job_data),
        status=interview.status,
        revision=interview.revision,
        question_count=len(questions),
        answered_count=len(answers),
        follow_up_count=sum(
            1
            for question in questions
            if question.kind == "follow_up"
            and any(answer.question_id == question.id for answer in answers)
        ),
        current_question=CurrentQuestion(
            id=current.id,
            ordinal=current.ordinal,
            kind=current.kind,
            text=current.text,
            topic_id=current.topic_id,
            source_answer_id=current.source_answer_id,
            delivery_status=current.delivery_status,
        )
        if current is not None
        else None,
        answerable_question_id=interview.answerable_question_id,
        rubric=list(signals),
        topics_covered=covered,
        gaps=[topic.id for topic in pack.topics if topic.id not in covered],
        skills_detected=[
            SkillDetected(skill_id=skill, evidence=[e for e in signal.evidence])
            for signal in signals
            for skill in signal.skill_ids
        ],
        decision=DecisionSummary(
            action=latest.action,
            reason_code=latest.reason_code,
            reason=latest.reason,
            topic_id=latest.topic_id,
            source_answer_id=latest.source_answer_id,
        )
        if latest is not None
        else None,
        evaluation_status=_evaluation_status(interview),
        last_error_code=interview.last_error_code,
        can_resume=interview.status in _UNFINISHED,
    )


class InterviewService:
    def __init__(
        self,
        db: SqlDBClientPort,
        llm: LLMPort | None,
        evaluation_llm: LLMPort | None = None,
    ):
        self._db = db
        self._llm = llm
        evaluator = evaluation_llm if evaluation_llm is not None else llm
        self._evaluation = (
            EvaluationService(db, evaluator) if evaluator is not None else None
        )
        self._policy = InterviewPolicy()

    async def create(
        self, owner_id: UUID, job_id: UUID, request_id: UUID
    ) -> tuple[InterviewState, bool]:
        try:
            async with self._db.begin_session() as session:
                existing = await session.scalar(
                    sa.select(Interview).where(
                        Interview.user_id == owner_id,
                        Interview.create_request_id == request_id,
                    )
                )
                if existing is not None:
                    if existing.job_id != job_id:
                        raise GenericException(
                            "Create request already used for another job", 409
                        )
                    return await build_interview_state(session, existing), False
                job = await session.get(Job, job_id)
                if job is None:
                    raise GenericException("Job not found", 404)
                pack = RolePack.model_validate(job.pack)
                snapshot: dict[str, object] = {
                    "job": JobSummary(
                        id=job.id,
                        slug=job.slug,
                        title=job.title,
                        description=job.description,
                        pack_version=job.pack_version,
                    ).model_dump(mode="json"),
                    "pack": pack.model_dump(mode="json"),
                }
                interview = Interview(
                    user_id=owner_id,
                    job_id=job.id,
                    create_request_id=request_id,
                    job_snapshot=snapshot,
                    status="created",
                    revision=0,
                    signals=[
                        RubricSignal(
                            topic_id=t.id,
                            status="unobserved",
                            skill_ids=[],
                            evidence=[],
                        ).model_dump(mode="json")
                        for t in pack.topics
                    ],
                )
                session.add(interview)
                await session.flush()
                state = await build_interview_state(session, interview)
            return state, True
        except IntegrityError:
            async with self._db.begin_session() as session:
                existing = await session.scalar(
                    sa.select(Interview).where(
                        Interview.user_id == owner_id,
                        Interview.create_request_id == request_id,
                    )
                )
                if existing is None:
                    raise
                if existing.job_id != job_id:
                    raise GenericException(
                        "Create request already used for another job", 409
                    )
                return await build_interview_state(session, existing), False

    async def get_state(self, owner_id: UUID, interview_id: UUID) -> InterviewState:
        async with self._db.begin_session() as session:
            return await build_interview_state(
                session, await get_owned_interview(session, owner_id, interview_id)
            )

    async def get_transcript(
        self, owner_id: UUID, interview_id: UUID
    ) -> TranscriptResponse:
        async with self._db.begin_session() as session:
            interview = await get_owned_interview(session, owner_id, interview_id)
            questions = await _questions(session, interview.id)
            answers = {
                answer.question_id: answer
                for answer in await _answers(session, interview.id)
            }
            entries = (
                await session.scalars(
                    sa.select(TranscriptEntry)
                    .where(TranscriptEntry.interview_id == interview.id)
                    .order_by(TranscriptEntry.sequence)
                )
            ).all()
            return TranscriptResponse(
                interview_id=interview.id,
                status=interview.status,
                questions=[
                    TranscriptQuestion(
                        id=q.id,
                        ordinal=q.ordinal,
                        kind=q.kind,
                        text=q.text,
                        topic_id=q.topic_id,
                        source_answer_id=q.source_answer_id,
                        source_quote=q.source_quote,
                        answer=TranscriptAnswer(
                            id=answers[q.id].id, text=answers[q.id].text
                        )
                        if q.id in answers
                        else None,
                    )
                    for q in questions
                ],
                messages=[
                    TranscriptMessage(
                        id=e.id,
                        sequence=e.sequence,
                        run_id=e.run_id,
                        sdk_message_id=e.sdk_message_id,
                        speaker=e.speaker,
                        question_id=e.question_id,
                        answer_id=e.answer_id,
                        kind=e.kind,
                        processing_status=e.processing_status,
                        text=e.text,
                        interrupted=e.interrupted,
                        text_source=e.text_source,
                    )
                    for e in entries
                ],
            )

    async def get_evaluation(
        self, owner_id: UUID, interview_id: UUID
    ) -> EvaluationResponse:
        async with self._db.begin_session() as session:
            interview = await get_owned_interview(session, owner_id, interview_id)
            status = _evaluation_status(interview)
            # EvaluationService owns claims; GET only describes the persisted result.
            return EvaluationResponse(
                status=status,
                result=Evaluation.model_validate(interview.evaluation)
                if status == "completed"
                else None,
                error_code=interview.last_error_code
                if status == "failed" and interview.status == "evaluation_failed"
                else ("evaluation_failed" if status == "failed" else None),
                can_retry=status == "failed",
            )

    async def evaluate(self, interview_id: UUID) -> EvaluationResponse:
        if self._evaluation is None:
            raise GenericException("LLM service unavailable", 503)
        return await self._evaluation.evaluate(interview_id)

    async def record_turn(
        self,
        interview_id: UUID,
        run_id: UUID,
        sdk_message_id: str,
        captured_question_id: UUID | None,
        text: str,
    ) -> RecordedTurn:
        async with self._db.begin_session() as session:
            interview = await session.scalar(
                sa.select(Interview)
                .where(Interview.id == interview_id)
                .with_for_update()
            )
            if interview is None:
                raise GenericException("Interview not found", 404)
            existing = await session.scalar(
                sa.select(TranscriptEntry).where(
                    TranscriptEntry.interview_id == interview_id,
                    TranscriptEntry.run_id == run_id,
                    TranscriptEntry.sdk_message_id == sdk_message_id,
                )
            )
            if existing is not None:
                return RecordedTurn(
                    existing.id, existing.question_id, "duplicate", interview.revision
                )
            if not _run_valid(interview, run_id):
                raise GenericException("Voice run is no longer current", 409)
            sequence = (
                await session.scalar(
                    sa.select(
                        sa.func.coalesce(sa.func.max(TranscriptEntry.sequence), 0)
                    ).where(TranscriptEntry.interview_id == interview.id)
                )
            ) or 0
            control = classify_control(text)
            active = interview.status in _UNFINISHED
            question = (
                await session.get(InterviewQuestion, captured_question_id)
                if captured_question_id is not None
                else None
            )
            if question is not None and question.interview_id != interview.id:
                raise GenericException("Question not found", 404)
            if (
                active
                and control == "answer"
                and captured_question_id is not None
                and question is None
            ):
                raise GenericException("Question not found", 404)
            can_answer = (
                active
                and control == "answer"
                and question is not None
                and (
                    question.id == interview.answerable_question_id
                    or question.delivery_status != "pending"
                )
            )
            kind: Literal["answer", "repeat", "stop", "prelude", "duplicate"] = (
                "prelude"
            )
            answer: InterviewAnswer | None = None
            if active and control == "stop":
                kind = "stop"
            elif (
                active
                and control == "repeat"
                and interview.answerable_question_id is not None
            ):
                kind = "repeat"
            elif can_answer and question is not None:
                kind = "answer"
                answer = await session.scalar(
                    sa.select(InterviewAnswer)
                    .where(InterviewAnswer.question_id == question.id)
                    .with_for_update()
                )
                if answer is None:
                    answer = InterviewAnswer(
                        question_id=question.id,
                        interview_id=interview.id,
                        text=text,
                        revision=1,
                    )
                    session.add(answer)
                    await session.flush()
                else:
                    answer.text = f"{answer.text}\n{text}"
                    answer.revision += 1
            entry = TranscriptEntry(
                interview_id=interview.id,
                sequence=sequence + 1,
                run_id=run_id,
                sdk_message_id=sdk_message_id,
                speaker="user",
                question_id=captured_question_id,
                answer_id=answer.id if answer is not None else None,
                kind="input" if kind == "answer" else "control",
                processing_status="pending" if kind == "answer" else "applied",
                text=text,
                interrupted=False,
                text_source="stt",
            )
            session.add(entry)
            interview.revision += 1
            if kind == "stop":
                interview.status = "cancelled"
                interview.finish_reason = "user_stop"
                interview.current_question_id = None
                interview.answerable_question_id = None
                interview.lease_expires_at = None
                interview.run_id = None
                session.add(
                    record_interview_decision(
                        interview, action="finish", code="user_stop"
                    )
                )
            elif kind == "repeat":
                repeated = await session.get(
                    InterviewQuestion, interview.answerable_question_id
                )
                session.add(
                    record_interview_decision(
                        interview,
                        action="ask",
                        code="repeat_requested",
                        question=repeated,
                    )
                )
            await session.flush()
            return RecordedTurn(
                entry.id, captured_question_id, kind, interview.revision
            )

    async def voice_state(
        self, interview_id: UUID, run_id: UUID
    ) -> VoiceRunState | None:
        """Read the durable run fence and revision used for speech and state hints."""
        async with self._db.begin_session() as session:
            interview = await session.get(Interview, interview_id)
            if interview is None:
                return None
            return VoiceRunState(
                status=interview.status,
                last_error_code=interview.last_error_code,
                revision=interview.revision,
                current_question_id=interview.current_question_id,
                answerable_question_id=interview.answerable_question_id,
                run_current=_run_valid(interview, run_id),
            )

    async def begin_question(
        self, interview_id: UUID, run_id: UUID, question_id: UUID
    ) -> bool:
        """Make an approved question answerable only once its speech actually starts."""
        async with self._db.begin_session() as session:
            interview = await session.scalar(
                sa.select(Interview)
                .where(Interview.id == interview_id)
                .with_for_update()
            )
            if (
                interview is None
                or not _run_valid(interview, run_id)
                or interview.status not in _UNFINISHED
                or interview.last_error_code is not None
            ):
                return False
            if interview.answerable_question_id == question_id:
                return True  # Repeating a begun question does not change the logical state.
            if interview.current_question_id != question_id:
                return False
            question = await session.get(InterviewQuestion, question_id)
            if question is None or question.interview_id != interview_id:
                return False
            if (
                await session.scalar(
                    sa.select(InterviewAnswer.id).where(
                        InterviewAnswer.question_id == question_id
                    )
                )
                is not None
            ):
                return False
            interview.answerable_question_id = question_id
            interview.status = "active"
            interview.revision += 1
            return True

    async def observe_assistant(
        self,
        interview_id: UUID,
        run_id: UUID,
        sdk_message_id: str,
        question_id: UUID | None,
        kind: Literal["question", "control", "closing"],
        text: str,
        interrupted: bool,
        text_source: Literal["tts_aligned", "sdk_estimate"],
    ) -> None:
        """Append one immutable SDK observation; a replaced run cannot change delivery."""
        async with self._db.begin_session() as session:
            interview = await session.scalar(
                sa.select(Interview)
                .where(Interview.id == interview_id)
                .with_for_update()
            )
            if interview is None:
                return
            existing = await session.scalar(
                sa.select(TranscriptEntry.id).where(
                    TranscriptEntry.interview_id == interview_id,
                    TranscriptEntry.run_id == run_id,
                    TranscriptEntry.sdk_message_id == sdk_message_id,
                )
            )
            if existing is not None:
                return
            question = (
                await session.get(InterviewQuestion, question_id)
                if question_id is not None
                else None
            )
            if question_id is not None and (
                question is None or question.interview_id != interview_id
            ):
                return
            current_run = _run_valid(interview, run_id)
            # An interrupted handle can have no message. Do not invent a transcript entry,
            # or lock an unbegun pending question as if it had been spoken.
            if not text and question is None:
                return
            if text:
                sequence = (
                    await session.scalar(
                        sa.select(
                            sa.func.coalesce(sa.func.max(TranscriptEntry.sequence), 0)
                        ).where(TranscriptEntry.interview_id == interview_id)
                    )
                ) or 0
                session.add(
                    TranscriptEntry(
                        interview_id=interview_id,
                        sequence=sequence + 1,
                        run_id=run_id,
                        sdk_message_id=sdk_message_id,
                        speaker="assistant",
                        question_id=question_id,
                        answer_id=None,
                        kind=kind,
                        processing_status="not_applicable",
                        text=text,
                        interrupted=interrupted,
                        text_source=text_source,
                    )
                )
            begun = question is not None and (
                interview.answerable_question_id == question_id
                or await session.scalar(
                    sa.select(InterviewAnswer.id).where(
                        InterviewAnswer.question_id == question_id
                    )
                )
                is not None
            )
            if question is not None and current_run and begun:
                delivery = (
                    "interrupted" if interrupted else "completed" if text else "failed"
                )
                if question.delivery_status != delivery:
                    question.delivery_status = delivery

    async def prepare_reply(
        self, interview_id: UUID, run_id: UUID, is_current: Callable[[], bool]
    ) -> ApprovedReply | None:
        """Assess each pending answer before speaking; never hold a session over a model call."""
        for _ in range(10):
            reply, more_pending = await self._prepare_once(
                interview_id, run_id, is_current
            )
            if not more_pending:
                return reply
        raise ValueError("More than ten distinct answers await assessment")

    async def _prepare_once(
        self, interview_id: UUID, run_id: UUID, is_current: Callable[[], bool]
    ) -> tuple[ApprovedReply | None, bool]:
        if not is_current():
            return None, False
        async with self._db.begin_session() as session:
            interview = await session.get(Interview, interview_id)
            if (
                interview is None
                or not _run_valid(interview, run_id)
                or interview.status not in _UNFINISHED
            ):
                return None, False
            snapshot, questions, answers = await _snapshot(session, interview)
            pending = list(
                (
                    await session.scalars(
                        sa.select(TranscriptEntry)
                        .where(
                            TranscriptEntry.interview_id == interview.id,
                            TranscriptEntry.processing_status == "pending",
                            TranscriptEntry.speaker == "user",
                            TranscriptEntry.answer_id.is_not(None),
                        )
                        .order_by(TranscriptEntry.sequence)
                    )
                ).all()
            )
            revision = interview.revision
            last = await session.scalar(
                sa.select(TranscriptEntry)
                .where(
                    TranscriptEntry.interview_id == interview.id,
                    TranscriptEntry.run_id == run_id,
                    TranscriptEntry.speaker == "user",
                )
                .order_by(TranscriptEntry.sequence.desc())
                .limit(1)
            )
            if (
                last is not None
                and classify_control(last.text) == "repeat"
                and last.answer_id is None
            ):
                repeated = next(
                    (q for q in questions if q.id == interview.answerable_question_id),
                    None,
                )
                if repeated is not None:
                    return ApprovedReply(repeated.id, "control", repeated.text), False
            answered_ids = {answer.question_id for answer in answers}
            current = next(
                (q for q in questions if q.id == interview.current_question_id), None
            )
            locked_question = (
                current is not None
                and current.id not in answered_ids
                and (
                    current.delivery_status != "pending"
                    or current.id == interview.answerable_question_id
                )
            )
            if not pending and current is not None and current.id not in answered_ids:
                return ApprovedReply(current.id, "question", current.text), False
            if not pending and answers:
                # All accepted answer text was previously assessed; never assess it twice.
                return None, False
            source = next(
                (
                    answer
                    for answer in answers
                    if pending and answer.id == pending[0].answer_id
                ),
                None,
            )
            if pending and source is None:
                raise ValueError("Pending transcript entry has no saved answer")
            planned_for = source.id if source is not None else None
            last_question = next(
                (
                    q
                    for q in questions
                    if source is not None and q.id == source.question_id
                ),
                None,
            )
            if source is not None and last_question is None:
                raise ValueError("Answer references a missing question")
            more_pending = len({entry.answer_id for entry in pending}) > 1
            assessment_only = locked_question or more_pending
        # The only operations between these sessions are provider calls; the session and lock are gone.
        deadline = asyncio.get_running_loop().time() + _LLM_SECONDS
        try:
            async with asyncio.timeout(_LLM_SECONDS):
                if source is None:
                    choice = await self._choose_bank(
                        snapshot, self._policy.next_step(snapshot).eligible_bank_ids
                    )
                    plan: TurnPlan | None = None
                else:
                    plan = await self._plan_turn(
                        snapshot,
                        source.id,
                        cast(InterviewQuestion, last_question).topic_id,
                        assessment_only,
                    )
                    choice = None
        except Exception as exc:
            # Invalid output is distinct from network/refusal/timeouts; do not fabricate speech.
            error = (
                "llm_invalid_output"
                if isinstance(exc, (ValueError, ValidationError))
                else "llm_unavailable"
            )
            await self._pause_failed_plan(
                interview_id, run_id, revision, is_current, error
            )
            return None, False
        # Validate the provider proposal before opening the write transaction. A bad
        # signal or question cannot leave partially updated signals/input markers.
        approved_signal: RubricSignal | None = None
        try:
            if source is not None:
                if plan is None:
                    raise ValueError("Missing assessment of the saved answer")
                approved_signal = plan.signal
                self._policy.validate_signal(approved_signal, snapshot)
                if (
                    approved_signal.topic_id
                    != cast(InterviewQuestion, last_question).topic_id
                ):
                    raise ValueError(
                        "Planning signal must match the just-answered topic"
                    )
                snapshot = replace(
                    snapshot,
                    signals=tuple(
                        approved_signal
                        if signal.topic_id == approved_signal.topic_id
                        else signal
                        for signal in snapshot.signals
                    ),
                )
            step = self._policy.next_step(snapshot)
            approved: tuple[str, str | None, str | None] | None = None
            if not assessment_only:
                if step.action == "finish":
                    if plan is not None and (
                        plan.bank_question_id is not None
                        or plan.follow_up_text is not None
                        or plan.source_quote is not None
                    ):
                        raise ValueError(
                            "Finished planning cannot propose an additional question"
                        )
                else:
                    if step.topic_id is None or step.kind is None:
                        raise ValueError("Ask decision must specify topic and kind")
                    if (
                        step.kind == "follow_up"
                        and len(snapshot.answers) >= 6
                        and plan is not None
                    ):
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise TimeoutError("Question planning deadline exceeded")
                        async with asyncio.timeout(remaining):
                            follow_up = await self._gap_follow_up(
                                step, snapshot, plan.signal
                            )
                        plan = plan.model_copy(
                            update={
                                "follow_up_text": follow_up.follow_up_text,
                                "source_quote": follow_up.source_quote,
                            }
                        )
                    approved = self._approved_question(step, snapshot, plan, choice)
        except ValueError, ValidationError:
            await self._pause_failed_plan(
                interview_id, run_id, revision, is_current, "llm_invalid_output"
            )
            return None, False
        except Exception:
            await self._pause_failed_plan(
                interview_id, run_id, revision, is_current, "llm_unavailable"
            )
            return None, False
        if not is_current():
            return None, False
        async with self._db.begin_session() as session:
            interview = await session.scalar(
                sa.select(Interview)
                .where(Interview.id == interview_id)
                .with_for_update()
            )
            if (
                interview is None
                or not _run_valid(interview, run_id)
                or interview.revision != revision
                or not is_current()
                or interview.status not in _UNFINISHED
            ):
                return None, False
            snapshot, questions, answers = await _snapshot(session, interview)
            if source is not None:
                if not any(
                    a.id == planned_for and a.text == source.text for a in answers
                ):
                    return None, False
                signal_update = cast(RubricSignal, approved_signal)
                interview.signals = [
                    (
                        signal_update
                        if signal.topic_id == signal_update.topic_id
                        else signal
                    ).model_dump(mode="json")
                    for signal in snapshot.signals
                ]
            if more_pending and source is not None:
                for entry in pending:
                    if entry.answer_id == source.id:
                        await session.execute(
                            sa.update(TranscriptEntry)
                            .where(
                                TranscriptEntry.id == entry.id,
                                TranscriptEntry.processing_status == "pending",
                            )
                            .values(processing_status="applied")
                        )
                source_question = cast(InterviewQuestion, last_question)
                code = (
                    "evidence_gap"
                    if source_question.ordinal > 6
                    else "required_follow_up"
                    if source_question.kind == "follow_up"
                    else "initial_topic"
                    if source_question.ordinal == 1
                    else "required_topic"
                )
                interview.last_error_code = None
                session.add(
                    record_interview_decision(
                        interview, action="ask", code=code, question=source_question
                    )
                )
                return None, True
            current = next(
                (q for q in questions if q.id == interview.current_question_id), None
            )
            begun = (
                current is not None
                and current.id not in {answer.question_id for answer in answers}
                and (
                    current.delivery_status != "pending"
                    or current.id == interview.answerable_question_id
                )
            )
            if begun and source is not None and current is not None:
                # Earlier speech can finish after the following question begins.
                # Its signal can change, but that already-heard question is immutable.
                for entry in pending:
                    await session.execute(
                        sa.update(TranscriptEntry)
                        .where(
                            TranscriptEntry.id == entry.id,
                            TranscriptEntry.processing_status == "pending",
                        )
                        .values(processing_status="applied")
                    )
                previous = await session.scalar(
                    sa.select(InterviewDecision)
                    .where(
                        InterviewDecision.interview_id == interview.id,
                        InterviewDecision.question_id == current.id,
                        InterviewDecision.reason_code != "repeat_requested",
                    )
                    .order_by(InterviewDecision.revision.desc())
                    .limit(1)
                )
                code = (
                    previous.reason_code
                    if previous is not None
                    else (
                        "evidence_gap"
                        if current.ordinal > 6
                        else "required_follow_up"
                        if current.kind == "follow_up"
                        else "initial_topic"
                        if current.ordinal == 1
                        else "required_topic"
                    )
                )
                interview.last_error_code = None
                session.add(
                    record_interview_decision(
                        interview, action="ask", code=code, question=current
                    )
                )
                return None, False
            if step.action == "finish":
                if source is not None:
                    for entry in pending:
                        await session.execute(
                            sa.update(TranscriptEntry)
                            .where(
                                TranscriptEntry.id == entry.id,
                                TranscriptEntry.processing_status == "pending",
                            )
                            .values(processing_status="applied")
                        )
                interview.status = "evaluating"
                interview.finish_reason = step.reason_code
                interview.current_question_id = None
                interview.answerable_question_id = None
                interview.last_error_code = None
                session.add(
                    record_interview_decision(
                        interview, action="finish", code=step.reason_code
                    )
                )
                return ApprovedReply(None, "closing", CLOSING_TEXT), False
            if step.kind is None or step.topic_id is None:
                raise ValueError("Ask decision must specify topic and kind")
            candidate_text, bank_id, quote = cast(
                tuple[str, str | None, str | None], approved
            )
            current = next(
                (q for q in questions if q.id == interview.current_question_id), None
            )
            revise = (
                current is not None
                and current.delivery_status == "pending"
                and current.id != interview.answerable_question_id
                and current.ordinal == len(answers) + 1
                and current.id not in {a.question_id for a in answers}
            )
            if revise and current is not None:
                question = current
                question.kind = step.kind
                question.topic_id = step.topic_id
                question.bank_question_id = bank_id
                question.source_answer_id = step.source_answer_id
                question.source_quote = quote
                question.text = candidate_text
            else:
                question = InterviewQuestion(
                    interview_id=interview.id,
                    ordinal=len(answers) + 1,
                    kind=step.kind,
                    topic_id=step.topic_id,
                    bank_question_id=bank_id,
                    source_answer_id=step.source_answer_id,
                    source_quote=quote,
                    text=candidate_text,
                    delivery_status="pending",
                )
                session.add(question)
                await session.flush()
            interview.current_question_id = question.id
            interview.status = "active"
            interview.last_error_code = None
            if source is not None:
                for entry in pending:
                    await session.execute(
                        sa.update(TranscriptEntry)
                        .where(
                            TranscriptEntry.id == entry.id,
                            TranscriptEntry.processing_status == "pending",
                        )
                        .values(processing_status="applied")
                    )
            session.add(
                record_interview_decision(
                    interview, action="ask", code=step.reason_code, question=question
                )
            )
            return ApprovedReply(question.id, "question", question.text), False

    async def _choose_bank(
        self, snapshot: InterviewSnapshot, eligible: tuple[str, ...]
    ) -> BankChoice:
        if not eligible:
            raise ValueError("No bank questions eligible")
        prompt = json.dumps(
            {
                "eligible_bank_ids": eligible,
                "topic": "ownership",
                "rule": "Select exactly one eligible ID; wording is supplied by the saved role pack.",
            }
        )
        return await self._structured(
            BankChoice,
            "Select only a role-pack bank question ID. Return strict JSON.",
            prompt,
            lambda choice: self._check_choice(choice.bank_question_id, eligible),
        )

    @staticmethod
    def _check_choice(bank_id: str, eligible: tuple[str, ...]) -> None:
        if bank_id not in eligible:
            raise ValueError("Bank ID was not eligible")

    async def _plan_turn(
        self,
        snapshot: InterviewSnapshot,
        answer_id: UUID,
        topic_id: str,
        assessment_only: bool,
    ) -> TurnPlan:
        topic = next(t for t in snapshot.pack.topics if t.id == topic_id)
        topic_answers = [a for a in snapshot.answers if a.topic_id == topic_id]
        scheduled = self._policy.next_step(snapshot)
        next_step_kind = (
            "assessment_only"
            if assessment_only
            else "follow_up"
            if len(snapshot.answers) in (1, 3)
            else "bank"
            if len(snapshot.answers) in (2, 4, 5)
            else "gap_assessment"
        )
        prompt = json.dumps(
            {
                "role_pack": snapshot.pack.model_dump(mode="json"),
                "just_answered_topic": topic.id,
                "just_answered_id": str(answer_id),
                "topic_answers": [
                    {"answer_id": str(a.answer_id), "text": a.answer_text}
                    for a in topic_answers
                ],
                "all_answers": [
                    {
                        "answer_id": str(a.answer_id),
                        "topic_id": a.topic_id,
                        "question_id": str(a.question_id),
                        "text": a.answer_text,
                    }
                    for a in snapshot.answers
                ],
                "previous_signals": [
                    s.model_dump(mode="json") for s in snapshot.signals
                ],
                "schedule": "The server fixes the schedule: ownership bank, answer-1 follow-up, technical-1 bank, answer-3 follow-up, technical-2 bank, behavioral-2 bank; after six ask only balanced insufficient-evidence probes, stop by ten. Re-assess only just_answered_topic. The resulting four signals determine the next step. If assessment_only is true, another question has begun or more saved answers await assessment: all three proposal fields must be null. Otherwise propose one bank ID only if the resulting step is a bank question, or a verbatim quote from the source answer in the gap/required follow-up topic only if the resulting step is a follow-up. On finish, all three proposal fields must be null.",
                "used_bank_ids": sorted(snapshot.used_bank_ids),
                "assessment_only": assessment_only,
                "next_step_kind": next_step_kind,
                "eligible_bank_question_ids": list(scheduled.eligible_bank_ids),
                "required_next_topic_id": (
                    scheduled.topic_id if len(snapshot.answers) < 6 else None
                ),
                "required_follow_up_source_answer_id": (
                    str(scheduled.source_answer_id)
                    if len(snapshot.answers) in (1, 3)
                    and scheduled.source_answer_id is not None
                    else None
                ),
            }
        )
        system = (
            "Return strict JSON TurnPlan. Evaluate only the supplied topic across its saved answers. "
            "An assessed signal requires verbatim same-topic answer citations; explicit negative evidence is assessable. "
            "Detect skills only for positive demonstrated evidence, not keyword mentions. "
            "Missing/ambiguous evidence is insufficient, not incompetence. Never infer personal traits. "
            "When next_step_kind is gap_assessment, return the topic signal but leave all "
            "question proposal fields null; the server selects the next gap topic and "
            "asks for a separate targeted follow-up only if one is needed. "
            "When next_step_kind is bank, bank_question_id MUST be one of the supplied "
            "eligible_bank_question_ids from required_next_topic_id, and both follow-up fields "
            "MUST be null. Never choose a bank ID from another role-pack topic. "
            "When next_step_kind is follow_up, bank_question_id MUST be null and both follow_up_text "
            "and source_quote MUST be non-null. Copy a short verbatim source_quote from the specified "
            "prior answer and include that identical quote in follow_up_text. "
            "Follow_up_text MUST be one plain-text question ending in exactly one question mark, "
            "at most 450 characters. Do not add an explanatory sentence. "
            "No invented candidate facts, answer hints, markdown, code blocks, or multiple questions. "
            "No intent, cancellation or extra fields."
        )
        return await self._structured(
            TurnPlan,
            system,
            prompt,
            lambda plan: self._check_plan_signal(
                plan, snapshot, topic_id, assessment_only
            ),
        )

    async def _gap_follow_up(
        self,
        step: NextStep,
        snapshot: InterviewSnapshot,
        signal: RubricSignal,
    ) -> _GapFollowUp:
        source = next(
            (
                answer
                for answer in snapshot.answers
                if answer.answer_id == step.source_answer_id
            ),
            None,
        )
        topic = next(
            (topic for topic in snapshot.pack.topics if topic.id == step.topic_id),
            None,
        )
        if source is None or topic is None:
            raise ValueError(
                "Gap follow-up requires a saved answer in the selected topic"
            )
        prompt = json.dumps(
            {
                "selected_topic_id": topic.id,
                "category": topic.category,
                "source_answer_id": str(source.answer_id),
                "source_answer": source.answer_text,
                "role_grounded_examples": [
                    question.text for question in topic.questions
                ],
            },
            ensure_ascii=False,
        )

        def grounded_question(value: _GapFollowUp) -> str:
            if value.source_quote in value.follow_up_text:
                return value.follow_up_text
            return f'You mentioned "{value.source_quote}" — {value.follow_up_text}'

        def validate(value: _GapFollowUp) -> None:
            candidate = TurnPlan(
                signal=signal,
                bank_question_id=None,
                follow_up_text=grounded_question(value),
                source_quote=value.source_quote,
            )
            self._approved_question(step, snapshot, candidate, None)

        proposal = await self._structured(
            _GapFollowUp,
            "Return strict JSON containing source_quote and follow_up_text. Copy a short "
            "verbatim quote from the given source_answer and generate exactly one neutral "
            "question about the selected role topic. The server will place the exact quote "
            "before the question if your question does not already contain it. "
            "End with exactly one question mark. Do not invent facts or answer hints.",
            prompt,
            validate,
        )
        return proposal.model_copy(
            update={"follow_up_text": grounded_question(proposal)}
        )

    def _check_plan_signal(
        self,
        plan: TurnPlan,
        snapshot: InterviewSnapshot,
        topic_id: str,
        assessment_only: bool,
    ) -> None:
        if plan.signal.topic_id != topic_id:
            raise ValueError("Signal topic does not match answer topic")
        if plan.signal.status == "unobserved":
            raise ValueError("An answered topic must be insufficient or assessed")
        self._policy.validate_signal(plan.signal, snapshot)
        if assessment_only:
            if any(
                value is not None
                for value in (
                    plan.bank_question_id,
                    plan.follow_up_text,
                    plan.source_quote,
                )
            ):
                raise ValueError(
                    "Assessment-only input cannot replace or plan a question"
                )
            return
        updated = replace(
            snapshot,
            signals=tuple(
                plan.signal if s.topic_id == topic_id else s for s in snapshot.signals
            ),
        )
        if len(snapshot.answers) >= 6:
            if any(
                value is not None
                for value in (
                    plan.bank_question_id,
                    plan.follow_up_text,
                    plan.source_quote,
                )
            ):
                raise ValueError("Gap assessment cannot preselect the next question")
            return
        step = self._policy.next_step(updated)
        if step.action == "ask" and step.kind == "follow_up":
            self._approved_question(step, updated, plan, None)
        else:
            self._check_plan_bank(step, plan)

    @staticmethod
    def _check_plan_bank(step: NextStep, plan: TurnPlan) -> None:
        target = step
        if target.action == "finish":
            if any(
                value is not None
                for value in (
                    plan.bank_question_id,
                    plan.follow_up_text,
                    plan.source_quote,
                )
            ):
                raise ValueError("No question is authorized after finish")
        elif target.kind == "bank":
            if (
                plan.bank_question_id not in target.eligible_bank_ids
                or plan.follow_up_text is not None
                or plan.source_quote is not None
            ):
                raise ValueError("Bank question choice is invalid")

    def _approved_question(
        self,
        step: NextStep,
        snapshot: InterviewSnapshot,
        plan: TurnPlan | None,
        choice: BankChoice | None,
    ) -> tuple[str, str | None, str | None]:
        target = step
        if target.kind == "bank":
            bank_id = (
                choice.bank_question_id
                if choice is not None
                else (plan.bank_question_id if plan is not None else None)
            )
            if bank_id not in target.eligible_bank_ids:
                raise ValueError("Bank question ID not eligible for this step")
            if plan is not None and (
                plan.follow_up_text is not None or plan.source_quote is not None
            ):
                raise ValueError("Bank proposal included follow-up data")
            question = next(
                (
                    q
                    for topic in snapshot.pack.topics
                    if topic.id == target.topic_id
                    for q in topic.questions
                    if q.id == bank_id
                ),
                None,
            )
            if question is None:
                raise ValueError("Bank question not in saved role pack")
            return question.text, bank_id, None
        if (
            plan is None
            or target.kind != "follow_up"
            or target.source_answer_id is None
        ):
            raise ValueError("Answer-linked follow-up is required")
        if (
            plan.bank_question_id is not None
            or plan.follow_up_text is None
            or plan.source_quote is None
        ):
            raise ValueError("Invalid follow-up proposal")
        source = next(
            (
                a
                for a in snapshot.answers
                if a.answer_id == target.source_answer_id
                and a.topic_id == target.topic_id
            ),
            None,
        )
        if source is None:
            raise ValueError("Follow-up source is not a previous answer in this topic")
        quote = plan.source_quote
        text = plan.follow_up_text.strip()
        if not quote.strip() or quote not in source.answer_text or quote not in text:
            raise ValueError(
                "Follow-up must contain a verbatim excerpt from its source answer"
            )
        if (
            len(text) > 450
            or len(text) < 1
            or text.count("?") != 1
            or not text.endswith("?")
            or "\n" in text
            or "```" in text
            or re.search(r"(^|\s)[#*]", text)
        ):
            raise ValueError(
                "Follow-up must be one plain-text question within 450 characters"
            )
        return text, None, quote

    async def _structured[T: BaseModel](
        self, schema: type[T], system: str, prompt: str, validate: Callable[[T], None]
    ) -> T:
        if self._llm is None:
            raise GenericException("LLM service unavailable", 503)
        last_invalid: Exception | None = None
        for attempt in range(2):
            text = (
                prompt
                if attempt == 0
                else f"{prompt}\nPrevious output was invalid: {last_invalid}. Correct the JSON strictly."
            )
            result = await self._llm.generate_completion(
                system, text, schema_output=schema.model_json_schema()
            )
            try:
                value = schema.model_validate(result)
                validate(value)
                return value
            except (ValidationError, ValueError) as exc:
                last_invalid = exc
        raise ValueError(
            "Model returned invalid interview planning output twice"
        ) from last_invalid

    async def _pause_failed_plan(
        self,
        interview_id: UUID,
        run_id: UUID,
        revision: int,
        is_current: Callable[[], bool],
        error: str,
    ) -> None:
        if not is_current():
            return
        async with self._db.begin_session() as session:
            interview = await session.scalar(
                sa.select(Interview)
                .where(Interview.id == interview_id)
                .with_for_update()
            )
            if (
                interview is None
                or not _run_valid(interview, run_id)
                or interview.revision != revision
                or interview.status not in _UNFINISHED
                or not is_current()
            ):
                return
            interview.status = "paused"
            interview.last_error_code = error
            session.add(
                record_interview_decision(
                    interview, action="pause", code="provider_unavailable"
                )
            )
