"""Durable progression when evidence is missing or a voice run is replaced."""

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from livekit.agents import AgentSession, llm
from livekit.agents.voice import SpeechHandle
from livekit.agents.voice.events import (
    CloseEvent,
    SpeechCreatedEvent,
    UserStateChangedEvent,
)
from livekit.plugins import openai

from app.exceptions import GenericException
from app.job_packs import seed_jobs
from app.ports import LLMPort, SqlDBClientPort
from app.usecases.auth import AuthUseCase
from app.usecases.interviews import ApprovedReply, InterviewService
from app.usecases.voice_sessions import ConnectionGrant, VoiceSessionService
from conf import settings
from domain.models import Interview, Job, User
from infra.voice.interviewer import InterviewerAgent

from .test_interviews import ScenarioLLM, remove_interview
from .test_voice_interview import RoomGateway

if TYPE_CHECKING:
    from entrypoints.api.http.fastapi_.factory import ASGIFactory
    from entrypoints.api.http.fastapi_.types_ import AsgiApp


class RecoveryLLM(ScenarioLLM):
    """Emit supported evidence and follow-ups from saved answers, not a fixed transcript."""

    def __init__(self, *, ambiguous_ownership: bool = False) -> None:
        self.ambiguous_ownership = ambiguous_ownership
        self.fail_next_turn = False
        self.calls = 0

    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: None = None,
        schema_output: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls += 1
        payload = json.loads(user_message)
        if "eligible_bank_ids" in payload:
            return {"bank_question_id": payload["eligible_bank_ids"][0]}
        if "selected_topic_id" in payload:
            source_text = payload["source_answer"]
            quote = source_text[: min(len(source_text), 80)]
            return {
                "source_quote": quote,
                "follow_up_text": "What concrete decision did you make in that situation?",
            }
        if "questions_and_answers" in payload:
            answers = payload["questions_and_answers"]
            signals = {
                signal["topic_id"]: signal for signal in payload["frozen_signals"]
            }
            return {
                "strengths": [],
                "concerns": [],
                "rubric_scores": [
                    {
                        "topic_id": topic["id"],
                        "score": (
                            0
                            if signals[topic["id"]]["status"] != "assessed"
                            or all(
                                answer["answer"] == "I don't know."
                                for answer in answers
                                if answer["topic_id"] == topic["id"]
                            )
                            else 3
                        ),
                        "answer_ids": (
                            [
                                next(
                                    answer["answer_id"]
                                    for answer in answers
                                    if answer["topic_id"] == topic["id"]
                                )
                            ]
                            if signals[topic["id"]]["status"] == "assessed"
                            else []
                        ),
                    }
                    for topic in payload["topics"]
                ],
            }
        if self.fail_next_turn:
            self.fail_next_turn = False
            raise ConnectionError("scripted model outage")
        topic_id: str = payload["just_answered_topic"]
        topic_answers = payload["topic_answers"]
        current = topic_answers[-1]
        assert current["answer_id"] == payload["just_answered_id"]
        same_topic_ids = {item["answer_id"] for item in topic_answers}
        assert same_topic_ids == {
            answer["answer_id"]
            for answer in payload["all_answers"]
            if answer["topic_id"] == topic_id
        }
        answer_count = len(payload["all_answers"])
        assert answer_count in range(1, 11)
        source: dict[str, str] | None = None
        bank_id: str | None = None
        if answer_count in (1, 3):
            source = current
        elif answer_count in (2, 4, 5):
            next_topic = {2: 1, 4: 2, 5: 3}[answer_count]
            bank_id = payload["role_pack"]["topics"][next_topic]["questions"][0]["id"]
        elif answer_count >= 6 and self.ambiguous_ownership:
            assert any(
                answer["topic_id"] == "ownership" for answer in payload["all_answers"]
            )
        elif answer_count != 6:
            raise AssertionError("Unexpected post-minimum probe for an assessed topic")
        if payload["assessment_only"]:
            source = None
            bank_id = None
        quote = source["text"] if source is not None else None
        if source is not None:
            matching = next(
                answer
                for answer in payload["all_answers"]
                if answer["answer_id"] == source["answer_id"]
            )
            assert matching["topic_id"] == (
                "ownership" if answer_count >= 6 else topic_id
            )
            assert quote and quote in matching["text"]
        return {
            "signal": {
                "topic_id": topic_id,
                "status": "insufficient"
                if self.ambiguous_ownership and topic_id == "ownership"
                else "assessed",
                "skill_ids": [],
                "evidence": [
                    {"answer_id": current["answer_id"], "quote": current["text"]}
                ],
            },
            "bank_question_id": bank_id,
            "follow_up_text": f'You mentioned "{quote}" — what happened next?'
            if quote is not None
            else None,
            "source_quote": quote,
        }


@dataclass(frozen=True)
class RecoveryCase:
    db: SqlDBClientPort
    owner_id: UUID
    interview_id: UUID


@pytest.fixture
async def recovery_case(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
) -> AsyncIterator[RecoveryCase]:
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
    ).create_user(f"recovery-{uuid4()}@example.com", "correct-horse-battery-staple")
    interview_id: UUID | None = None
    try:
        state, created = await InterviewService(db, None).create(
            user.id, job_id, uuid4()
        )
        assert created
        interview_id = state.id
        yield RecoveryCase(db, user.id, interview_id)
    finally:
        if interview_id is not None:
            await remove_interview(db, interview_id, user.id)
        else:
            async with db.begin_session() as session:
                await session.execute(sa.delete(User).where(User.id == user.id))


async def _leased_run(case: RecoveryCase) -> UUID:
    run_id = uuid4()
    async with case.db.begin_session() as session:
        interview = await session.get(Interview, case.interview_id)
        assert interview is not None
        interview.run_id = run_id
        interview.status = "active"
        interview.lease_expires_at = datetime.now(UTC) + timedelta(minutes=10)
    return run_id


async def _answer(
    case: RecoveryCase,
    service: InterviewService,
    run_id: UUID,
    reply: ApprovedReply,
    text: str,
    sequence: int,
) -> None:
    assert reply.kind == "question" and reply.question_id is not None
    assert await service.begin_question(case.interview_id, run_id, reply.question_id)
    recorded = await service.record_turn(
        case.interview_id, run_id, f"answer-{sequence}", reply.question_id, text
    )
    assert recorded.kind == "answer"


async def test_ambiguous_evidence_probes_same_topic_until_limit_and_preserves_gap(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    llm = RecoveryLLM(ambiguous_ownership=True)
    service = InterviewService(case.db, cast(LLMPort, llm))
    run_id = await _leased_run(case)
    reply = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    for ordinal in range(1, 11):
        assert reply is not None and reply.kind == "question"
        assert reply.question_id is not None
        if ordinal >= 7:
            state = await service.get_state(case.owner_id, case.interview_id)
            assert (
                state.decision is not None
                and state.decision.reason_code == "evidence_gap"
            )
            assert state.decision.topic_id == "ownership"
            transcript = await service.get_transcript(case.owner_id, case.interview_id)
            question = transcript.questions[ordinal - 1]
            source = next(
                q.answer
                for q in transcript.questions
                if q.answer is not None and q.answer.id == question.source_answer_id
            )
            assert source is not None
            assert question.kind == "follow_up" and question.topic_id == "ownership"
            assert (
                question.source_quote is not None
                and question.source_quote in source.text
            )
            assert question.source_quote in question.text
            previous = transcript.questions[1 if ordinal == 7 else ordinal - 2].answer
            assert previous is not None
            assert question.source_answer_id == previous.id
        spoken = {
            1: "I need more context on the ownership decision.",
            2: "I still cannot say what I personally decided.",
            3: "I persist idempotency keys with request hashes to avoid duplicate orders.",
            4: "I reject retries when the request hash differs for the same key.",
            5: "I use transaction locks to protect reservations of the last unit.",
            6: "I traced an incident through request logs and added query metrics.",
        }.get(ordinal, f"I cannot clarify my ownership actions for example {ordinal}.")
        await _answer(case, service, run_id, reply, spoken, ordinal)
        reply = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    assert reply is not None and reply.kind == "closing"
    state = await service.get_state(case.owner_id, case.interview_id)
    assert (
        state.status == "evaluating"
        and state.answered_count == state.question_count == 10
    )
    assert state.follow_up_count == 6
    assert state.gaps == ["ownership"]
    assert state.decision is not None and state.decision.reason_code == "question_limit"
    evaluation = await service.evaluate(case.interview_id)
    assert evaluation.status == "completed" and evaluation.result is not None
    assert evaluation.result.insufficient_evidence
    assert evaluation.result.gaps == ["ownership"]
    assert (
        next(
            row
            for row in evaluation.result.rubric_scores
            if row.topic_id == "ownership"
        ).score
        == 0
    )
    assert evaluation.result.overall_score == 60


async def test_explicit_negative_evidence_stops_at_six_without_extra_probe(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    service = InterviewService(case.db, cast(LLMPort, RecoveryLLM()))
    run_id = await _leased_run(case)
    reply = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    for ordinal in range(1, 7):
        assert reply is not None
        await _answer(case, service, run_id, reply, "I don't know.", ordinal)
        reply = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    assert reply is not None and reply.kind == "closing"
    state = await service.get_state(case.owner_id, case.interview_id)
    assert (
        state.status == "evaluating"
        and state.question_count == state.answered_count == 6
    )
    assert state.gaps == [] and state.topics_covered == [
        "ownership",
        "api_design",
        "data_consistency",
        "reliability",
    ]
    assert (
        state.decision is not None and state.decision.reason_code == "coverage_complete"
    )
    transcript = await service.get_transcript(case.owner_id, case.interview_id)
    assert all(
        question.answer is not None and question.answer.text == "I don't know."
        for question in transcript.questions
    )
    evaluation = await service.evaluate(case.interview_id)
    assert evaluation.status == "completed" and evaluation.result is not None
    assert evaluation.result.overall_score == 0
    assert evaluation.result.gaps == [] and not evaluation.result.insufficient_evidence
    assert all(row.score == 0 for row in evaluation.result.rubric_scores)


async def test_saved_answer_resumes_after_model_failure_with_new_run_without_respeaking(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    llm = RecoveryLLM()
    service = InterviewService(case.db, cast(LLMPort, llm))
    gateway = RoomGateway()
    gateway.proceed.set()
    voice = VoiceSessionService(
        case.db, gateway, server_url="wss://test.livekit.invalid"
    )
    first = await voice.connect(case.owner_id, case.interview_id, uuid4())
    assert isinstance(first, ConnectionGrant)
    first_question = await service.prepare_reply(
        case.interview_id, first.run_id, lambda: True
    )
    assert first_question is not None
    await _answer(
        case,
        service,
        first.run_id,
        first_question,
        "I mediated the ownership trade-off.",
        1,
    )
    llm.fail_next_turn = True
    assert (
        await service.prepare_reply(case.interview_id, first.run_id, lambda: True)
        is None
    )
    paused = await service.get_state(case.owner_id, case.interview_id)
    assert paused.status == "paused" and paused.last_error_code == "llm_unavailable"
    assert paused.answered_count == paused.question_count == 1
    saved = await service.get_transcript(case.owner_id, case.interview_id)
    assert saved.questions[0].id == first_question.question_id
    assert saved.questions[0].answer is not None
    answer_id = saved.questions[0].answer.id
    assert saved.questions[0].answer.text == "I mediated the ownership trade-off."
    assert len(saved.messages) == 1 and saved.messages[0].processing_status == "pending"
    assert saved.messages[0].answer_id == answer_id

    resumed = await voice.connect(case.owner_id, case.interview_id, uuid4())
    assert isinstance(resumed, ConnectionGrant) and resumed.run_id != first.run_id
    assert first.room_name not in gateway.rooms
    assert (
        await service.prepare_reply(case.interview_id, first.run_id, lambda: True)
        is None
    )
    next_reply = await service.prepare_reply(
        case.interview_id, resumed.run_id, lambda: True
    )
    assert next_reply is not None and next_reply.kind == "question"
    recovered = await service.get_transcript(case.owner_id, case.interview_id)
    assert recovered.questions[0].id == first_question.question_id
    assert (
        recovered.questions[0].answer is not None
        and recovered.questions[0].answer.id == answer_id
    )
    assert recovered.questions[0].answer.text == "I mediated the ownership trade-off."
    assert (
        len(recovered.questions) == 2
        and recovered.questions[1].id == next_reply.question_id
    )
    assert recovered.questions[1].source_answer_id == answer_id
    assert (
        len(recovered.messages) == 1
        and recovered.messages[0].processing_status == "applied"
    )
    again = await service.prepare_reply(case.interview_id, resumed.run_id, lambda: True)
    assert again is not None and again.question_id == next_reply.question_id
    assert (
        await service.get_state(case.owner_id, case.interview_id)
    ).question_count == 2
    assert len(gateway.dispatches) == 2


async def test_repeat_and_stop_skip_model_while_saved_answer_is_pending(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    llm = RecoveryLLM()
    service = InterviewService(case.db, cast(LLMPort, llm))
    run_id = await _leased_run(case)
    first = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    assert first is not None and first.question_id is not None
    await _answer(case, service, run_id, first, "I coordinated the API trade-off.", 1)
    calls_before_controls = llm.calls
    repeated = await service.record_turn(
        case.interview_id,
        run_id,
        "repeat-control",
        first.question_id,
        "Please repeat the question?!",
    )
    assert repeated.kind == "repeat"
    replay = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    assert replay is not None and replay.kind == "control"
    assert replay.question_id == first.question_id and replay.text == first.text
    assert llm.calls == calls_before_controls
    still_pending = await service.get_transcript(case.owner_id, case.interview_id)
    assert (
        len(still_pending.questions) == 1
        and still_pending.questions[0].answer is not None
    )
    assert [message.processing_status for message in still_pending.messages] == [
        "pending",
        "applied",
    ]
    stopped = await service.record_turn(
        case.interview_id,
        run_id,
        "stop-control",
        first.question_id,
        "Please stop the interview.",
    )
    assert stopped.kind == "stop"
    assert await service.prepare_reply(case.interview_id, run_id, lambda: True) is None
    assert llm.calls == calls_before_controls
    cancelled = await service.get_state(case.owner_id, case.interview_id)
    assert cancelled.status == "cancelled" and cancelled.answered_count == 1
    assert cancelled.question_count == 1 and cancelled.evaluation_status == "not_ready"
    saved = await service.get_transcript(case.owner_id, case.interview_id)
    assert [message.processing_status for message in saved.messages] == [
        "pending",
        "applied",
        "applied",
    ]
    assert (
        saved.questions[0].answer is not None
        and saved.questions[0].answer.text == "I coordinated the API trade-off."
    )


async def test_sdk_six_finalized_answers_persist_closing_and_evaluate_once(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    model = RecoveryLLM()
    interviews = InterviewService(case.db, cast(LLMPort, model))
    run_id = await _leased_run(case)
    agent = InterviewerAgent(
        case.interview_id,
        run_id,
        "sdk-six-turns",
        interviews,
        VoiceSessionService(case.db, None),
    )
    sdk_llm = openai.LLM(
        model="gpt-4.1-mini",
        api_key="inert-local-only",
        base_url="http://127.0.0.1:1",
        max_retries=0,
    )
    handles: list[SpeechHandle] = []
    async with AgentSession(llm=sdk_llm) as session:
        agent.bind_session(session)

        def captured(event: SpeechCreatedEvent) -> None:
            handles.append(event.speech_handle)

        session.on("speech_created", captured)
        try:
            await session.start(agent, record=False)
            await agent.start_question()
            assert len(handles) == 1
            await asyncio.wait_for(handles[0], timeout=10)
            answers = (
                "I owned the migration, scoped the rollout, and measured error rates.",
                "I rolled it back when error rates exceeded the threshold.",
                "I used idempotency keys to avoid duplicate orders on retries.",
                "I rejected a retry if its request hash did not match the original.",
                "I used transactions and locks to avoid overselling the last unit.",
                "I traced an outage through logs and then added preventive alerts.",
            )
            for ordinal, answer in enumerate(answers, 1):
                async with asyncio.timeout(5):
                    while True:
                        state = await interviews.get_state(
                            case.owner_id, case.interview_id
                        )
                        if (
                            state.current_question is not None
                            and state.answerable_question_id
                            == state.current_question.id
                        ):
                            assert state.current_question.ordinal == ordinal
                            break
                        await asyncio.sleep(0.02)
                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="listening", new_state="speaking"),
                )
                chat = llm.ChatContext()
                message = chat.add_message(role="user", content=answer)
                await agent.on_user_turn_completed(chat, message)
                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="speaking", new_state="listening"),
                )
                reply = session.generate_reply(user_input=message)
                await asyncio.wait_for(reply, timeout=10)
                assert reply.exception() is None
            async with asyncio.timeout(10):
                while not await agent.interview_finished():
                    await asyncio.sleep(0.02)
        finally:
            await asyncio.wait_for(agent.close(), timeout=10)

    finished = await interviews.get_state(case.owner_id, case.interview_id)
    transcript = await interviews.get_transcript(case.owner_id, case.interview_id)
    evaluation = await interviews.get_evaluation(case.owner_id, case.interview_id)
    assert finished.status == "completed"
    assert (
        finished.decision is not None
        and finished.decision.reason_code == "coverage_complete"
    )
    assert finished.question_count == finished.answered_count == 6
    assert finished.follow_up_count == 2 and finished.answerable_question_id is None
    assert len(transcript.questions) == 6
    assert [
        question.answer.text for question in transcript.questions if question.answer
    ] == list(answers)
    assert [(entry.speaker, entry.kind) for entry in transcript.messages][-1] == (
        "assistant",
        "closing",
    )
    assert len([entry for entry in transcript.messages if entry.kind == "closing"]) == 1
    assert evaluation.status == "completed" and evaluation.result is not None
    assert evaluation.result.overall_score > 0


async def test_late_finalized_speech_does_not_replace_a_question_already_heard(
    recovery_case: RecoveryCase,
) -> None:
    class DelayedTurnLLM(RecoveryLLM):
        async def generate_completion(
            self,
            system_message: str,
            user_message: str,
            tools: None = None,
            schema_output: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload = json.loads(user_message)
            if "just_answered_id" in payload and (
                payload["just_answered_id"] != payload["topic_answers"][-1]["answer_id"]
            ):
                source = next(
                    item
                    for item in payload["topic_answers"]
                    if item["answer_id"] == payload["just_answered_id"]
                )
                return {
                    "signal": {
                        "topic_id": payload["just_answered_topic"],
                        "status": "assessed",
                        "skill_ids": [],
                        "evidence": [
                            {"answer_id": source["answer_id"], "quote": source["text"]}
                        ],
                    },
                    "bank_question_id": (
                        payload["eligible_bank_question_ids"][0]
                        if not payload["assessment_only"]
                        else None
                    ),
                    "follow_up_text": None,
                    "source_quote": None,
                }
            return await super().generate_completion(
                system_message, user_message, tools, schema_output
            )

    case = recovery_case
    service = InterviewService(case.db, cast(LLMPort, DelayedTurnLLM()))
    run_id = await _leased_run(case)
    first = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    assert first is not None and first.question_id is not None
    assert await service.begin_question(case.interview_id, run_id, first.question_id)
    await service.observe_assistant(
        case.interview_id,
        run_id,
        "spoken-first",
        first.question_id,
        "question",
        first.text,
        False,
        "sdk_estimate",
    )
    initial_answer = "I owned the integration and monitored every deployment."
    recorded = await service.record_turn(
        case.interview_id,
        run_id,
        "initial-first",
        first.question_id,
        initial_answer,
    )
    assert recorded.kind == "answer"
    second = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    assert second is not None and second.question_id is not None
    assert await service.begin_question(case.interview_id, run_id, second.question_id)
    await service.observe_assistant(
        case.interview_id,
        run_id,
        "spoken-second",
        second.question_id,
        "question",
        second.text,
        False,
        "sdk_estimate",
    )
    second_answer = "I rolled back after monitoring showed errors."
    assert (
        await service.record_turn(
            case.interview_id,
            run_id,
            "initial-second",
            second.question_id,
            second_answer,
        )
    ).kind == "answer"
    late_first = "I then measured that retries stayed below the threshold."
    assert (
        await service.record_turn(
            case.interview_id,
            run_id,
            "late-first",
            first.question_id,
            late_first,
        )
    ).kind == "answer"

    third = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    assert (
        third is not None and third.question_id is not None and third.kind == "question"
    )
    assert third.question_id not in {first.question_id, second.question_id}
    assert await service.begin_question(case.interview_id, run_id, third.question_id)
    await service.observe_assistant(
        case.interview_id,
        run_id,
        "spoken-third",
        third.question_id,
        "question",
        third.text,
        False,
        "sdk_estimate",
    )

    late_second = "I validated that the rollback fixed error rates."
    assert (
        await service.record_turn(
            case.interview_id,
            run_id,
            "late-second",
            second.question_id,
            late_second,
        )
    ).kind == "answer"
    # Its assessment may update evidence, but Q3 is already answerable and immutable.
    assert await service.prepare_reply(case.interview_id, run_id, lambda: True) is None
    state = await service.get_state(case.owner_id, case.interview_id)
    transcript = await service.get_transcript(case.owner_id, case.interview_id)
    assert state.question_count == 3 and state.answered_count == 2
    assert state.current_question is not None
    assert (state.current_question.id, state.current_question.text) == (
        third.question_id,
        third.text,
    )
    assert state.answerable_question_id == third.question_id
    assert transcript.questions[0].answer is not None
    assert transcript.questions[0].answer.text == f"{initial_answer}\n{late_first}"
    assert transcript.questions[1].answer is not None
    assert transcript.questions[1].answer.text == f"{second_answer}\n{late_second}"
    assert all(
        entry.processing_status == "applied"
        for entry in transcript.messages
        if entry.speaker == "user"
    )


async def test_failed_room_close_keeps_cancelled_interview_and_can_be_retried(
    recovery_case: RecoveryCase,
) -> None:
    class FlakyRoomGateway(RoomGateway):
        fail_close = True

        async def close_room(self, room_name: str) -> None:
            if self.fail_close:
                raise ConnectionError("temporary room cleanup failure")
            await super().close_room(room_name)

    case = recovery_case
    gateway = FlakyRoomGateway()
    gateway.proceed.set()
    voice = VoiceSessionService(
        case.db, gateway, server_url="wss://test.livekit.invalid"
    )
    grant = await voice.connect(case.owner_id, case.interview_id, uuid4())
    assert isinstance(grant, ConnectionGrant)
    assert grant.room_name in gateway.rooms

    with pytest.raises(GenericException) as failed:
        await voice.end(case.owner_id, case.interview_id)
    assert failed.value.code == 503
    state = await InterviewService(case.db, None).get_state(
        case.owner_id, case.interview_id
    )
    assert state.status == "cancelled" and not state.can_resume
    assert state.decision is not None and state.decision.reason_code == "user_stop"
    assert state.last_error_code == "voice_dispatch_failed"
    assert grant.room_name in gateway.rooms
    assert not await voice.claim_run(case.interview_id, grant.run_id, "late-worker")

    gateway.fail_close = False
    ended = await voice.end(case.owner_id, case.interview_id)
    assert ended.status == "cancelled" and not ended.can_resume
    assert grant.room_name not in gateway.rooms
    assert ended.decision is not None and ended.decision.reason_code == "user_stop"


async def test_sdk_planner_outage_pauses_with_saved_answer_and_resumes_without_respeaking(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    gateway = RoomGateway()
    gateway.proceed.set()
    voice = VoiceSessionService(
        case.db, gateway, server_url="wss://test.livekit.invalid"
    )
    llm_model = RecoveryLLM()
    interviews = InterviewService(case.db, cast(LLMPort, llm_model))
    first_grant = await voice.connect(case.owner_id, case.interview_id, uuid4())
    assert isinstance(first_grant, ConnectionGrant)
    first_job = "sdk-planner-outage"
    assert await voice.claim_run(case.interview_id, first_grant.run_id, first_job)
    agent = InterviewerAgent(
        case.interview_id,
        first_grant.run_id,
        first_job,
        interviews,
        voice,
    )
    sdk_llm = openai.LLM(
        model="gpt-4.1-mini",
        api_key="inert-local-only",
        base_url="http://127.0.0.1:1",
        max_retries=0,
    )
    handles: list[SpeechHandle] = []
    closed = asyncio.Event()
    async with AgentSession(llm=sdk_llm) as session:
        agent.bind_session(session)

        def capture(event: SpeechCreatedEvent) -> None:
            handles.append(event.speech_handle)

        def marked_closed(event: CloseEvent) -> None:
            closed.set()

        session.on("speech_created", capture)
        session.on("close", marked_closed)
        try:
            await session.start(agent, record=False)
            await agent.start_question()
            assert len(handles) == 1
            await asyncio.wait_for(handles[0], timeout=10)
            async with asyncio.timeout(5):
                while True:
                    state = await interviews.get_state(case.owner_id, case.interview_id)
                    if (
                        state.current_question is not None
                        and state.answerable_question_id == state.current_question.id
                    ):
                        break
                    await asyncio.sleep(0.02)
            question_id = state.answerable_question_id
            session.emit(
                "user_state_changed",
                UserStateChangedEvent(old_state="listening", new_state="speaking"),
            )
            ctx = llm.ChatContext()
            reply = ctx.add_message(
                role="user", content="I owned the migration and verified error rates."
            )
            await agent.on_user_turn_completed(ctx, reply)
            session.emit(
                "user_state_changed",
                UserStateChangedEvent(old_state="speaking", new_state="listening"),
            )
            llm_model.fail_next_turn = True
            handle = session.generate_reply(user_input=reply)
            await asyncio.wait_for(handle, timeout=10)
            await asyncio.wait_for(closed.wait(), timeout=5)
        finally:
            await asyncio.wait_for(agent.close(), timeout=10)

    paused = await interviews.get_state(case.owner_id, case.interview_id)
    saved = await interviews.get_transcript(case.owner_id, case.interview_id)
    assert paused.status == "paused" and paused.last_error_code == "llm_unavailable"
    assert paused.answered_count == paused.question_count == 1 and paused.can_resume
    assert saved.questions[0].answer is not None
    answer_id = saved.questions[0].answer.id
    assert len([entry for entry in saved.messages if entry.speaker == "user"]) == 1
    assert (
        next(
            entry for entry in saved.messages if entry.speaker == "user"
        ).processing_status
        == "pending"
    )

    grant = await voice.connect(case.owner_id, case.interview_id, uuid4())
    assert isinstance(grant, ConnectionGrant)
    assert grant.run_id != first_grant.run_id
    assert await voice.claim_run(case.interview_id, grant.run_id, "sdk-resume")
    resumed_agent = InterviewerAgent(
        case.interview_id,
        grant.run_id,
        "sdk-resume",
        interviews,
        voice,
    )
    resumed_handles: list[SpeechHandle] = []
    async with AgentSession() as session:
        resumed_agent.bind_session(session)

        def capture_resumed(event: SpeechCreatedEvent) -> None:
            resumed_handles.append(event.speech_handle)

        session.on("speech_created", capture_resumed)
        try:
            await session.start(resumed_agent, record=False)
            await resumed_agent.start_question()
            assert len(resumed_handles) == 1
            await asyncio.wait_for(resumed_handles[0], timeout=10)
        finally:
            await asyncio.wait_for(resumed_agent.close(), timeout=10)
    recovered = await interviews.get_state(case.owner_id, case.interview_id)
    transcript = await interviews.get_transcript(case.owner_id, case.interview_id)
    assert recovered.question_count == 2 and recovered.answered_count == 1
    assert recovered.current_question is not None
    assert recovered.current_question.kind == "follow_up"
    assert recovered.current_question.source_answer_id == answer_id
    assert transcript.questions[0].id == question_id
    assert transcript.questions[0].answer is not None
    assert transcript.questions[0].answer.id == answer_id
    assert len([entry for entry in transcript.messages if entry.speaker == "user"]) == 1


async def test_vad_false_start_before_first_question_cannot_steal_its_answer(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    interviews = InterviewService(case.db, cast(LLMPort, RecoveryLLM()))
    run_id = await _leased_run(case)
    agent = InterviewerAgent(
        case.interview_id,
        run_id,
        "sdk-vad-alignment",
        interviews,
        VoiceSessionService(case.db, None),
    )
    handles: list[SpeechHandle] = []
    async with AgentSession() as session:
        agent.bind_session(session)

        def created(event: SpeechCreatedEvent) -> None:
            handles.append(event.speech_handle)

        session.on("speech_created", created)
        try:
            await session.start(agent, record=False)
            # A brief pre-question VAD start has no finalized STT callback.
            session.emit(
                "user_state_changed",
                UserStateChangedEvent(
                    old_state="listening",
                    new_state="speaking",
                    created_at=1000.0,
                ),
            )
            session.emit(
                "user_state_changed",
                UserStateChangedEvent(
                    old_state="speaking",
                    new_state="listening",
                    created_at=1000.1,
                ),
            )
            await agent.start_question()
            assert len(handles) == 1
            await asyncio.wait_for(handles[0], timeout=10)
            async with asyncio.timeout(5):
                while True:
                    before = await interviews.get_state(
                        case.owner_id, case.interview_id
                    )
                    if (
                        before.current_question is not None
                        and before.answerable_question_id == before.current_question.id
                    ):
                        break
                    await asyncio.sleep(0.02)
            question_id = before.answerable_question_id
            session.emit(
                "user_state_changed",
                UserStateChangedEvent(
                    old_state="listening",
                    new_state="speaking",
                    created_at=2000.0,
                ),
            )
            chat = llm.ChatContext()
            message = chat.add_message(
                role="user", content="I owned the migration and monitored its outcome."
            )
            message.metrics["started_speaking_at"] = 2000.0
            await agent.on_user_turn_completed(chat, message)
            session.emit(
                "user_state_changed",
                UserStateChangedEvent(
                    old_state="speaking",
                    new_state="listening",
                    created_at=2000.1,
                ),
            )
        finally:
            await asyncio.wait_for(agent.close(), timeout=10)
    saved = await interviews.get_state(case.owner_id, case.interview_id)
    transcript = await interviews.get_transcript(case.owner_id, case.interview_id)
    assert saved.question_count == saved.answered_count == 1
    assert transcript.questions[0].id == question_id
    assert transcript.questions[0].answer is not None
    assert transcript.questions[0].answer.text == message.text_content
    assert [(entry.speaker, entry.kind) for entry in transcript.messages] == [
        ("assistant", "question"),
        ("user", "input"),
    ]
    assert transcript.messages[1].question_id == question_id
    assert transcript.messages[1].answer_id == transcript.questions[0].answer.id


async def test_voice_lease_lost_while_candidate_speaks_cannot_schedule_a_question(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    gateway = RoomGateway()
    gateway.proceed.set()
    voice = VoiceSessionService(
        case.db, gateway, server_url="wss://test.livekit.invalid"
    )
    llm_model = RecoveryLLM()
    interviews = InterviewService(case.db, cast(LLMPort, llm_model))
    grant = await voice.connect(case.owner_id, case.interview_id, uuid4())
    assert isinstance(grant, ConnectionGrant)
    worker_job = "candidate-still-speaking"
    assert await voice.claim_run(case.interview_id, grant.run_id, worker_job)
    agent = InterviewerAgent(
        case.interview_id,
        grant.run_id,
        worker_job,
        interviews,
        voice,
    )
    seen: list[SpeechHandle] = []
    async with AgentSession() as session:
        agent.bind_session(session)

        def captured(event: SpeechCreatedEvent) -> None:
            seen.append(event.speech_handle)

        session.on("speech_created", captured)
        try:
            await session.start(agent, record=False)
            session.emit(
                "user_state_changed",
                UserStateChangedEvent(old_state="listening", new_state="speaking"),
            )
            planning = asyncio.create_task(agent.start_question())
            await voice.release_run(
                case.interview_id, grant.run_id, worker_job, "disconnect"
            )
            await asyncio.wait_for(planning, timeout=5)
        finally:
            await asyncio.wait_for(agent.close(), timeout=10)

    state = await interviews.get_state(case.owner_id, case.interview_id)
    transcript = await interviews.get_transcript(case.owner_id, case.interview_id)
    assert state.status == "paused" and state.can_resume
    assert state.question_count == state.answered_count == 0
    assert state.answerable_question_id is None and state.current_question is None
    assert not seen and not transcript.messages
    assert llm_model.calls == 0


async def test_saved_answer_advances_if_sdk_never_schedules_its_reply(
    recovery_case: RecoveryCase,
) -> None:
    case = recovery_case
    interviews = InterviewService(case.db, cast(LLMPort, RecoveryLLM()))
    run_id = await _leased_run(case)
    agent = InterviewerAgent(
        case.interview_id,
        run_id,
        "missing-sdk-generation",
        interviews,
        VoiceSessionService(case.db, None),
    )
    handles: list[SpeechHandle] = []
    second_speech = asyncio.Event()
    async with AgentSession() as session:
        agent.bind_session(session)

        def captured(event: SpeechCreatedEvent) -> None:
            handles.append(event.speech_handle)
            if len(handles) == 2:
                second_speech.set()

        session.on("speech_created", captured)
        try:
            await session.start(agent, record=False)
            await agent.start_question()
            assert len(handles) == 1
            await asyncio.wait_for(handles[0], timeout=10)
            async with asyncio.timeout(5):
                while True:
                    first = await interviews.get_state(case.owner_id, case.interview_id)
                    if (
                        first.current_question is not None
                        and first.answerable_question_id == first.current_question.id
                    ):
                        break
                    await asyncio.sleep(0.02)
            session.emit(
                "user_state_changed",
                UserStateChangedEvent(old_state="listening", new_state="speaking"),
            )
            chat = llm.ChatContext()
            answer = chat.add_message(
                role="user",
                content="I owned the rollout and negotiated rollback criteria with another team.",
            )
            await agent.on_user_turn_completed(chat, answer)
            session.emit(
                "user_state_changed",
                UserStateChangedEvent(old_state="speaking", new_state="listening"),
            )
            # No session.generate_reply: the fallback must recover from saved input.
            await asyncio.wait_for(second_speech.wait(), timeout=10)
            await asyncio.wait_for(handles[1], timeout=10)
        finally:
            await asyncio.wait_for(agent.close(), timeout=10)

    state = await interviews.get_state(case.owner_id, case.interview_id)
    transcript = await interviews.get_transcript(case.owner_id, case.interview_id)
    assert state.question_count == 2 and state.answered_count == 1
    assert (
        state.current_question is not None
        and state.current_question.kind == "follow_up"
    )
    assert state.current_question.id != first.current_question.id
    assert transcript.questions[0].answer is not None
    assert transcript.questions[0].answer.text == answer.text_content
    assert transcript.questions[1].source_answer_id == transcript.questions[0].answer.id
    assert (
        len(
            [
                entry
                for entry in transcript.messages
                if entry.speaker == "assistant"
                and entry.question_id == state.current_question.id
            ]
        )
        == 1
    )


async def test_late_answer_extension_revises_same_unspoken_followup(
    recovery_case: RecoveryCase,
) -> None:
    class RevisedFollowUpLLM(RecoveryLLM):
        async def generate_completion(
            self,
            system_message: str,
            user_message: str,
            tools: None = None,
            schema_output: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            payload = json.loads(user_message)
            if (
                payload.get("next_step_kind") == "follow_up"
                and len(payload["all_answers"]) == 1
            ):
                source = payload["topic_answers"][-1]
                if "\n" in source["text"]:
                    quote = source["text"].splitlines()[-1]
                    return {
                        "signal": {
                            "topic_id": payload["just_answered_topic"],
                            "status": "assessed",
                            "skill_ids": [],
                            "evidence": [
                                {"answer_id": source["answer_id"], "quote": quote}
                            ],
                        },
                        "bank_question_id": None,
                        "source_quote": quote,
                        "follow_up_text": f'You mentioned "{quote}" — how did you validate that result?',
                    }
            return await super().generate_completion(
                system_message,
                user_message,
                tools,
                schema_output,
            )

    case = recovery_case
    service = InterviewService(case.db, cast(LLMPort, RevisedFollowUpLLM()))
    run_id = await _leased_run(case)
    first = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    assert first is not None and first.question_id is not None
    assert await service.begin_question(case.interview_id, run_id, first.question_id)
    initial_text = "I negotiated an API rollout with the partner team."
    assert (
        await service.record_turn(
            case.interview_id,
            run_id,
            "initial-answer",
            first.question_id,
            initial_text,
        )
    ).kind == "answer"
    original_next = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    assert original_next is not None and original_next.question_id is not None
    assert original_next.kind == "question"
    before = await service.get_state(case.owner_id, case.interview_id)
    assert before.question_count == 2 and before.answered_count == 1
    assert before.answerable_question_id == first.question_id
    assert before.current_question is not None
    assert before.current_question.delivery_status == "pending"

    late_detail = "I measured errors and got agreement on the rollback trigger."
    assert (
        await service.record_turn(
            case.interview_id,
            run_id,
            "late-detail",
            first.question_id,
            late_detail,
        )
    ).kind == "answer"
    revised = await service.prepare_reply(case.interview_id, run_id, lambda: True)
    assert revised is not None and revised.kind == "question"
    assert revised.question_id == original_next.question_id
    assert revised.text != original_next.text
    assert late_detail in revised.text
    after = await service.get_state(case.owner_id, case.interview_id)
    transcript = await service.get_transcript(case.owner_id, case.interview_id)
    assert after.question_count == 2 and after.answered_count == 1
    assert after.current_question is not None
    assert after.current_question.id == original_next.question_id
    assert after.answerable_question_id == first.question_id
    assert len(transcript.questions) == 2
    assert transcript.questions[0].answer is not None
    assert transcript.questions[0].answer.text == f"{initial_text}\n{late_detail}"
    assert transcript.questions[1].source_quote == late_detail
    assert transcript.questions[1].text == revised.text
    assert all(
        item.processing_status == "applied"
        for item in transcript.messages
        if item.speaker == "user"
    )
    assert not any(
        item.speaker == "assistant" and item.question_id == revised.question_id
        for item in transcript.messages
    )
