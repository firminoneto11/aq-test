"""Roomless SDK speech events; this does not exercise STT, VAD, TTS, or room audio."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from time import time
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from livekit.agents import AgentSession, llm, stt, tts
from livekit.agents.voice import SpeechHandle
from livekit.agents.voice.events import (
    CloseEvent,
    ErrorEvent,
    SpeechCreatedEvent,
    UserStateChangedEvent,
)
from livekit.plugins import openai

from app.job_packs import seed_jobs
from app.ports import LLMPort, SqlDBClientPort
from app.usecases.auth import AuthUseCase
from app.usecases.interviews import InterviewService
from app.usecases.voice_sessions import VoiceSessionService
from conf import settings
from domain.models import Interview, Job, User
from infra.database.adapter import SqlDBClientAdapter
from infra.voice.interviewer import InterviewerAgent

from .test_interviews import ScenarioLLM, remove_interview
from .test_voice_interview import RoomGateway

if TYPE_CHECKING:
    from entrypoints.api.http.fastapi_.factory import ASGIFactory
    from entrypoints.api.http.fastapi_.types_ import AsgiApp


async def test_sdk_say_interrupted_before_output_replays_one_durable_question(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
) -> None:
    """Real speech handles bind before say returns; an unplayed question stays pending."""
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
    ).create_user(
        f"sdk-controller-{uuid4()}@example.com", "correct-horse-battery-staple"
    )
    interview_id: UUID | None = None
    reader = SqlDBClientAdapter(settings.DATABASE_URL, settings.AUTOCOMMIT)
    try:
        await reader.connect()
        writer = InterviewService(db, cast(LLMPort, ScenarioLLM()))
        durable = InterviewService(reader, None)
        state, created = await writer.create(user.id, job_id, uuid4())
        assert created
        interview_id = state.id
        run_id = uuid4()
        async with db.begin_session() as session:
            interview = await session.get(Interview, interview_id)
            assert interview is not None
            interview.run_id = run_id
            interview.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

        # The first speech is cancelled synchronously at the *actual SDK event*,
        # before text can be forwarded. No question may become answerable.
        first_handles: list[SpeechHandle] = []
        first_sources: list[str] = []
        first_agent = InterviewerAgent(
            interview_id,
            run_id,
            "sdk-local-first",
            writer,
            VoiceSessionService(db, None),
        )
        async with AgentSession() as session:
            first_agent.bind_session(session)

            def interrupt_at_creation(event: SpeechCreatedEvent) -> None:
                first_sources.append(event.source)
                first_handles.append(event.speech_handle)
                event.speech_handle.interrupt(force=True)

            session.on("speech_created", interrupt_at_creation)
            await session.start(first_agent, record=False)
            await first_agent.start_question()
            assert len(first_handles) == 1
            await first_handles[0]
            await first_agent.close()

        after_interrupt = await durable.get_state(user.id, interview_id)
        assert first_sources == ["say"]
        assert first_handles[0].interrupted
        assert after_interrupt.current_question is not None
        question_id = after_interrupt.current_question.id
        assert after_interrupt.current_question.ordinal == 1
        assert after_interrupt.current_question.topic_id == "ownership"
        assert after_interrupt.current_question.delivery_status == "pending"
        assert after_interrupt.answerable_question_id is None
        assert after_interrupt.question_count == 1
        assert after_interrupt.answered_count == 0
        assert (await durable.get_transcript(user.id, interview_id)).messages == []

        # A fresh SDK session must replay that ID; the speaking event commits
        # answerability and the handle's observed assistant item persists once.
        replay_handles: list[SpeechHandle] = []
        replay_sources: list[str] = []
        replay_agent = InterviewerAgent(
            interview_id,
            run_id,
            "sdk-local-replay",
            writer,
            VoiceSessionService(db, None),
        )
        async with AgentSession() as session:
            replay_agent.bind_session(session)

            def track_replay(event: SpeechCreatedEvent) -> None:
                replay_sources.append(event.source)
                replay_handles.append(event.speech_handle)

            session.on("speech_created", track_replay)
            await session.start(replay_agent, record=False)
            await replay_agent.start_question()
            assert len(replay_handles) == 1
            await replay_handles[0]
            await replay_agent.close()

        recovered = await durable.get_state(user.id, interview_id)
        transcript = await durable.get_transcript(user.id, interview_id)
        assert replay_sources == ["say"]
        assert replay_handles[0].exception() is None
        assert recovered.question_count == 1
        assert recovered.answered_count == 0
        assert recovered.answerable_question_id == question_id
        assert recovered.current_question is not None
        assert recovered.current_question.id == question_id
        assert recovered.current_question.delivery_status == "completed"
        assert len(transcript.questions) == 1
        assert transcript.questions[0].answer is None
        assert [
            (entry.speaker, entry.question_id, entry.kind, entry.text)
            for entry in transcript.messages
        ] == [
            ("assistant", question_id, "question", recovered.current_question.text),
        ]
        assert transcript.messages[0].text_source == "sdk_estimate"
    finally:
        await reader.disconnect()
        if interview_id is not None:
            await remove_interview(db, interview_id, user.id)
        else:
            async with db.begin_session() as session:
                await session.execute(sa.delete(User).where(User.id == user.id))


@pytest.mark.parametrize("failure_kind", ["stt", "tts"])
async def test_sdk_prequest_speech_and_provider_failure_preserve_unanswered_question_on_resume(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
    failure_kind: str,
) -> None:
    """Speech before playout is not an answer; STT/TTS failures release the lease."""
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
    ).create_user(
        f"sdk-{failure_kind}-{uuid4()}@example.com", "correct-horse-battery-staple"
    )
    interview_id: UUID | None = None
    reader = SqlDBClientAdapter(settings.DATABASE_URL, settings.AUTOCOMMIT)
    try:
        await reader.connect()
        interviews = InterviewService(db, cast(LLMPort, ScenarioLLM()))
        durable = InterviewService(reader, None)
        state, created = await interviews.create(user.id, job_id, uuid4())
        assert created
        interview_id = state.id
        gateway = RoomGateway()
        gateway.proceed.set()
        voice = VoiceSessionService(
            db, gateway, server_url="wss://test.livekit.invalid"
        )
        first_grant = await voice.connect(user.id, interview_id, uuid4())
        first_run = first_grant.run_id
        first_job = f"sdk-{failure_kind}-first"
        assert await voice.claim_run(interview_id, first_run, first_job)

        agent = InterviewerAgent(interview_id, first_run, first_job, interviews, voice)
        first_handles: list[SpeechHandle] = []
        async with AgentSession() as session:
            agent.bind_session(session)
            closed = asyncio.Event()

            def mark_closed(event: CloseEvent) -> None:
                closed.set()

            session.on("close", mark_closed)

            def interrupt_at_creation(event: SpeechCreatedEvent) -> None:
                first_handles.append(event.speech_handle)
                event.speech_handle.interrupt(force=True)

            session.on("speech_created", interrupt_at_creation)
            try:
                await session.start(agent, record=False)
                # SDK speech-start captures the currently answerable question, not
                # a question that might be approved or start playing afterward.
                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="listening", new_state="speaking"),
                )
                turn = llm.ChatContext()
                prelude = turn.add_message(
                    role="user", content="I would coordinate with the other team."
                )
                await agent.on_user_turn_completed(turn, prelude)
                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="speaking", new_state="listening"),
                )
                await agent.start_question()
                assert len(first_handles) == 1
                await first_handles[0]
                assert first_handles[0].interrupted

                pending = await durable.get_state(user.id, interview_id)
                assert pending.current_question is not None
                question_id = pending.current_question.id
                assert pending.question_count == 1
                assert pending.answered_count == 0
                assert pending.answerable_question_id is None
                assert pending.current_question.delivery_status == "pending"
                messages = (
                    await durable.get_transcript(user.id, interview_id)
                ).messages
                assert [
                    (item.speaker, item.question_id, item.answer_id, item.text)
                    for item in messages
                ] == [
                    ("user", None, None, prelude.text_content),
                ]
                assert messages[0].processing_status == "applied"

                error_type = stt.STTError if failure_kind == "stt" else tts.TTSError
                session.emit(
                    "error",
                    ErrorEvent(
                        error=error_type(
                            timestamp=time(),
                            label=f"test-{failure_kind}",
                            error=RuntimeError("offline"),
                            recoverable=False,
                        ),
                        source=f"test-{failure_kind}",
                    ),
                )
                await asyncio.wait_for(closed.wait(), timeout=5)
            finally:
                await asyncio.wait_for(agent.close(), timeout=10)

        paused = await durable.get_state(user.id, interview_id)
        assert paused.status == "paused"
        assert paused.can_resume
        assert paused.last_error_code == f"{failure_kind}_unavailable"
        assert paused.answered_count == 0
        # Waited for the SDK observer in close(): a cancelled, textless handle
        # must not create an assistant utterance after the earlier snapshot.
        before_resume = await durable.get_transcript(user.id, interview_id)
        assert [
            (item.speaker, item.question_id, item.answer_id)
            for item in before_resume.messages
        ] == [
            ("user", None, None),
        ]
        async with db.begin_session() as session:
            interview = await session.get(Interview, interview_id)
            assert interview is not None
            assert interview.lease_expires_at is None
            assert interview.worker_job_id is None
        assert not await voice.renew_lease(interview_id, first_run, first_job)

        second_grant = await voice.connect(user.id, interview_id, uuid4())
        assert second_grant.run_id != first_run
        second_job = "sdk-stt-resume"
        assert await voice.claim_run(interview_id, second_grant.run_id, second_job)
        resumed_agent = InterviewerAgent(
            interview_id,
            second_grant.run_id,
            second_job,
            interviews,
            voice,
        )
        replay_handles: list[SpeechHandle] = []
        async with AgentSession() as session:
            resumed_agent.bind_session(session)

            def capture_replay(event: SpeechCreatedEvent) -> None:
                replay_handles.append(event.speech_handle)

            session.on("speech_created", capture_replay)
            try:
                await session.start(resumed_agent, record=False)
                await resumed_agent.start_question()
                assert len(replay_handles) == 1
                await replay_handles[0]
            finally:
                await resumed_agent.close()

        recovered = await durable.get_state(user.id, interview_id)
        transcript = await durable.get_transcript(user.id, interview_id)
        assert recovered.question_count == 1
        assert recovered.answered_count == 0
        assert recovered.current_question is not None
        assert recovered.current_question.id == question_id
        assert recovered.answerable_question_id == question_id
        assert recovered.current_question.delivery_status == "completed"
        assert transcript.questions[0].answer is None
        assert [
            (item.speaker, item.question_id, item.kind) for item in transcript.messages
        ] == [
            ("user", None, "control"),
            ("assistant", question_id, "question"),
        ]
        assert transcript.messages[1].text == recovered.current_question.text
    finally:
        await reader.disconnect()
        if interview_id is not None:
            await remove_interview(db, interview_id, user.id)
        else:
            async with db.begin_session() as session:
                await session.execute(sa.delete(User).where(User.id == user.id))


async def test_sdk_finalized_speech_asks_evidence_grounded_followup_and_repeats_without_recount(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
) -> None:
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
    ).create_user(f"sdk-turn-{uuid4()}@example.com", "correct-horse-battery-staple")
    interview_id: UUID | None = None
    reader = SqlDBClientAdapter(settings.DATABASE_URL, settings.AUTOCOMMIT)
    try:
        await reader.connect()
        interviews = InterviewService(db, cast(LLMPort, ScenarioLLM()))
        durable = InterviewService(reader, None)
        created, is_new = await interviews.create(user.id, job_id, uuid4())
        assert is_new
        interview_id = created.id
        run_id = uuid4()
        async with db.begin_session() as session:
            interview = await session.get(Interview, interview_id)
            assert interview is not None
            interview.run_id = run_id
            interview.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)

        agent = InterviewerAgent(
            interview_id,
            run_id,
            "sdk-local-turn",
            interviews,
            VoiceSessionService(db, None),
        )
        handles: list[SpeechHandle] = []
        answer = (
            "I led the incident response, compared the request logs against our deployment, "
            "rolled back the unsafe release, and verified that error rates recovered."
        )
        # The SDK requires an LLM object for generate_reply; the agent's
        # llm_node supplies all text, so an inaccessible local endpoint catches
        # any accidental fallback to unsanctioned model generations.
        sdk_llm = openai.LLM(
            model="gpt-4.1-mini",
            api_key="inert-local-only",
            base_url="http://127.0.0.1:1",
            max_retries=0,
        )
        async with AgentSession(llm=sdk_llm) as session:
            agent.bind_session(session)

            def track_speech(event: SpeechCreatedEvent) -> None:
                handles.append(event.speech_handle)

            session.on("speech_created", track_speech)
            try:
                await session.start(agent, record=False)
                await agent.start_question()
                assert len(handles) == 1
                await asyncio.wait_for(handles[0], timeout=10)
                first = await durable.get_state(user.id, interview_id)
                assert first.current_question is not None
                first_id = first.current_question.id
                async with asyncio.timeout(5):
                    while first.answerable_question_id != first_id:
                        await asyncio.sleep(0.02)
                        first = await durable.get_state(user.id, interview_id)
                assert first.answerable_question_id == first_id

                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="listening", new_state="speaking"),
                )
                context = llm.ChatContext()
                statement = context.add_message(role="user", content=answer)
                await agent.on_user_turn_completed(context, statement)
                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="speaking", new_state="listening"),
                )
                next_speech = session.generate_reply(user_input=statement)
                await asyncio.wait_for(next_speech, timeout=10)
                assert next_speech.exception() is None
                after_answer = await durable.get_state(user.id, interview_id)
                assert after_answer.question_count == 2
                assert after_answer.answered_count == 1
                assert (
                    after_answer.follow_up_count == 0
                )  # Count only answered follow-ups.
                assert after_answer.current_question is not None
                assert after_answer.current_question.kind == "follow_up"
                followup_id = after_answer.current_question.id
                assert followup_id != first_id
                async with asyncio.timeout(5):
                    while after_answer.answerable_question_id != followup_id:
                        await asyncio.sleep(0.02)
                        after_answer = await durable.get_state(user.id, interview_id)
                assert after_answer.answerable_question_id == followup_id
                transcript = await durable.get_transcript(user.id, interview_id)
                assert transcript.questions[0].answer is not None
                assert transcript.questions[0].answer.text == answer
                assert (
                    transcript.questions[1].source_answer_id
                    == transcript.questions[0].answer.id
                )
                quote = transcript.questions[1].source_quote
                assert quote is not None and quote in answer
                assert quote in transcript.questions[1].text

                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="listening", new_state="speaking"),
                )
                repeat_context = llm.ChatContext()
                repeat = repeat_context.add_message(
                    role="user", content="repeat the question"
                )
                await agent.on_user_turn_completed(repeat_context, repeat)
                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="speaking", new_state="listening"),
                )
                repeated_speech = session.generate_reply(user_input=repeat)
                await asyncio.wait_for(repeated_speech, timeout=10)
                assert repeated_speech.exception() is None
            finally:
                await asyncio.wait_for(agent.close(), timeout=10)

        final = await durable.get_state(user.id, interview_id)
        transcript = await durable.get_transcript(user.id, interview_id)
        assert final.question_count == 2 and final.answered_count == 1
        assert final.current_question is not None
        assert final.current_question.id == followup_id
        assert final.answerable_question_id == followup_id
        assert (
            final.decision is not None
            and final.decision.reason_code == "repeat_requested"
        )
        assert [(entry.speaker, entry.kind) for entry in transcript.messages] == [
            ("assistant", "question"),
            ("user", "input"),
            ("assistant", "question"),
            ("user", "control"),
            ("assistant", "control"),
        ]
        assert transcript.questions[0].answer is not None
        assert transcript.messages[1].answer_id == transcript.questions[0].answer.id
        assert transcript.messages[1].processing_status == "applied"
        assert transcript.messages[-1].text == transcript.questions[1].text
    finally:
        await reader.disconnect()
        if interview_id is not None:
            await remove_interview(db, interview_id, user.id)
        else:
            async with db.begin_session() as session:
                await session.execute(sa.delete(User).where(User.id == user.id))


async def test_vad_only_start_cannot_strand_first_saved_answer(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
) -> None:
    """An epoch-invalidated planner must recover without a second finalized turn."""

    class HeldTurnLLM(ScenarioLLM):
        def __init__(self) -> None:
            self.entered = asyncio.Event()
            self.proceed = asyncio.Event()
            self.turn_calls = 0

        async def generate_completion(
            self,
            system_message: str,
            user_message: str,
            tools: None = None,
            schema_output: dict[str, Any] | None = None,
        ) -> dict[str, Any]:
            if "just_answered_id" in json.loads(user_message):
                self.turn_calls += 1
                if self.turn_calls == 1:
                    self.entered.set()
                    await self.proceed.wait()
            return await super().generate_completion(
                system_message,
                user_message,
                tools,
                schema_output,
            )

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
    ).create_user(
        f"sdk-vad-recovery-{uuid4()}@example.com", "correct-horse-battery-staple"
    )
    interview_id: UUID | None = None
    try:
        model = HeldTurnLLM()
        interviews = InterviewService(db, cast(LLMPort, model))
        created, is_new = await interviews.create(user.id, job_id, uuid4())
        assert is_new
        interview_id = created.id
        run_id = uuid4()
        async with db.begin_session() as session:
            interview = await session.get(Interview, interview_id)
            assert interview is not None
            interview.run_id = run_id
            interview.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
        agent = InterviewerAgent(
            interview_id,
            run_id,
            "sdk-vad-recovery",
            interviews,
            VoiceSessionService(db, None),
        )
        handles: list[tuple[str, SpeechHandle]] = []
        recovered = asyncio.Event()
        sdk_llm = openai.LLM(
            model="gpt-4.1-mini",
            api_key="inert-local-only",
            base_url="http://127.0.0.1:1",
            max_retries=0,
        )
        async with AgentSession(llm=sdk_llm) as session:
            agent.bind_session(session)

            def capture(event: SpeechCreatedEvent) -> None:
                handles.append((event.source, event.speech_handle))
                if len(handles) == 3:
                    recovered.set()

            session.on("speech_created", capture)
            try:
                await session.start(agent, record=False)
                await agent.start_question()
                assert len(handles) == 1
                await asyncio.wait_for(handles[0][1], timeout=10)
                first = await interviews.get_state(user.id, interview_id)
                assert first.current_question is not None
                first_id = first.current_question.id
                async with asyncio.timeout(5):
                    while first.answerable_question_id != first_id:
                        await asyncio.sleep(0.02)
                        first = await interviews.get_state(user.id, interview_id)
                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="listening", new_state="speaking"),
                )
                ctx = llm.ChatContext()
                answer = "I owned the API rollout, negotiated a trade-off, and tracked error rates."
                message = ctx.add_message(role="user", content=answer)
                await agent.on_user_turn_completed(ctx, message)
                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="speaking", new_state="listening"),
                )
                first_reply = session.generate_reply(user_input=message)
                await asyncio.wait_for(model.entered.wait(), timeout=5)
                # SDK user_state_changed is synchronous; this start produces no
                # completed STT hook, yet invalidates the in-flight first plan.
                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="listening", new_state="speaking"),
                )
                session.emit(
                    "user_state_changed",
                    UserStateChangedEvent(old_state="speaking", new_state="listening"),
                )
                pending = await interviews.get_state(user.id, interview_id)
                saved = await interviews.get_transcript(user.id, interview_id)
                assert pending.status == "active" and pending.last_error_code is None
                assert pending.question_count == pending.answered_count == 1
                assert [
                    item.processing_status
                    for item in saved.messages
                    if item.speaker == "user"
                ] == [
                    "pending",
                ]
                model.proceed.set()
                await asyncio.wait_for(first_reply, timeout=10)
                await asyncio.wait_for(recovered.wait(), timeout=10)
                assert [source for source, _ in handles] == [
                    "say",
                    "generate_reply",
                    "say",
                ]
                await asyncio.wait_for(handles[2][1], timeout=10)
                after = await interviews.get_state(user.id, interview_id)
                assert after.current_question is not None
                second_id = after.current_question.id
                async with asyncio.timeout(5):
                    while after.answerable_question_id != second_id:
                        await asyncio.sleep(0.02)
                        after = await interviews.get_state(user.id, interview_id)
                assert second_id != first_id
                assert after.question_count == 2 and after.answered_count == 1
                assert after.current_question is not None
                assert after.current_question.kind == "follow_up"
                assert after.last_error_code is None
                assert (
                    model.turn_calls == 2
                )  # Stale proposal discarded; saved text assessed once.
                transcript = await interviews.get_transcript(user.id, interview_id)
                assert transcript.questions[0].answer is not None
                assert transcript.questions[0].answer.text == answer
                assert (
                    transcript.questions[1].source_answer_id
                    == transcript.questions[0].answer.id
                )
                quote = transcript.questions[1].source_quote
                assert quote is not None and quote in answer
                assert [
                    item.processing_status
                    for item in transcript.messages
                    if item.speaker == "user"
                ] == [
                    "applied",
                ]
                assert (
                    len(
                        [
                            item
                            for item in transcript.messages
                            if item.speaker == "assistant"
                            and item.question_id == second_id
                        ]
                    )
                    == 1
                )
            finally:
                model.proceed.set()
                await asyncio.wait_for(agent.close(), timeout=10)
    finally:
        if interview_id is not None:
            await remove_interview(db, interview_id, user.id)
        else:
            async with db.begin_session() as session:
                await session.execute(sa.delete(User).where(User.id == user.id))
