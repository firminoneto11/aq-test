"""LiveKit speech coordination over the durable interview state machine."""

import asyncio
import json
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterable, Coroutine
from uuid import UUID

from livekit.agents import (
    Agent, AgentSession, ModelSettings, llm, stt, tts,
)
from livekit.agents.types import TimedString
from livekit.agents.voice import SpeechHandle
from livekit.agents.voice.events import (
    AgentStateChangedEvent, CloseEvent, ErrorEvent, SpeechCreatedEvent,
    UserStateChangedEvent,
)

from app.usecases.interviews import ApprovedReply, InterviewService
from app.usecases.voice_sessions import VoiceSessionService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _ReplySlot:
    message_id: str
    epoch: int
    enabled: bool
    handle: SpeechHandle | None = None
    settled: asyncio.Event = field(default_factory=asyncio.Event)
    reply: ApprovedReply | None = None
    begun: asyncio.Task[bool] | None = None
    aligned: bool = False
    estimated: bool = False


class InterviewerAgent(Agent):
    """Persist finalized input before authorizing any next question for synthesis."""

    def __init__(
        self, interview_id: UUID, run_id: UUID, job_id: str,
        interviews: InterviewService, voice: VoiceSessionService,
    ) -> None:
        super().__init__(instructions="Speak only the exact server-approved interview text.", tools=[])
        self._interview_id = interview_id
        self._run_id = run_id
        self._job_id = job_id
        self._interviews = interviews
        self._voice = voice
        self._session: AgentSession[None] | None = None
        self._epoch = 0
        self._finalized_turns = 0
        self._seen_turn_ids: set[str] = set()
        self._speaking = False
        self._silence = asyncio.Event()
        self._silence.set()
        self._captured: deque[tuple[float, UUID | None, int]] = deque()
        self._answerable: UUID | None = None
        self._reply_queue: deque[_ReplySlot] = deque()
        self._reply_by_id: dict[str, _ReplySlot] = {}
        self._handles: dict[str, _ReplySlot] = {}
        self._observers: dict[str, asyncio.Task[None]] = {}
        self._say_slot: _ReplySlot | None = None
        self._planning = asyncio.Lock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._closing_handle: SpeechHandle | None = None
        self._closing_scheduled = False
        self._evaluation_started = False
        self._closing_observed = False
        self._stopping = False
        self._closed = False

    def bind_session(self, session: AgentSession[None]) -> None:
        if self._session is not None:
            raise RuntimeError("Interviewer session is already bound")
        self._session = session
        session.on("user_state_changed", self._on_user_state)
        session.on("agent_state_changed", self._on_agent_state)
        session.on("speech_created", self._on_speech_created)
        session.on("error", self._on_error)
        session.on("close", self._on_session_close)

    def _retain[T](self, coro: Coroutine[Any, Any, T]) -> asyncio.Task[T]:
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def completed(done: asyncio.Task[T]) -> None:
            self._tasks.discard(done)
            if not done.cancelled():
                try:
                    done.result()
                except Exception:
                    logger.exception("Interview background operation failed")

        task.add_done_callback(completed)
        return task

    def _on_user_state(self, event: UserStateChangedEvent) -> None:
        if event.new_state == "speaking":
            self._epoch += 1  # Synchronous: fences a previous model call before the next hook runs.
            self._speaking = True
            self._silence.clear()
            self._captured.append((event.created_at, self._answerable, self._epoch))
        else:
            self._speaking = False
            self._silence.set()
            if event.new_state == "away" and not self._stopping:
                self._retain(self._end_media("idle"))

    def _on_session_close(self, event: CloseEvent) -> None:
        self._stopping = True
        self._epoch += 1
        self._silence.set()

    def _on_speech_created(self, event: SpeechCreatedEvent) -> None:
        if event.source == "say":
            slot = self._say_slot
            self._say_slot = None
        else:
            slot = self._reply_queue.popleft() if self._reply_queue else None
        if slot is None:
            # No speculative/manual generations are permitted in this controller.
            event.speech_handle.interrupt(force=True)
            return
        handle = event.speech_handle
        slot.handle = handle
        self._handles[handle.id] = slot
        handle.add_done_callback(self._on_speech_done)

    def _on_agent_state(self, event: AgentStateChangedEvent) -> None:
        if event.new_state != "speaking" or self._session is None:
            return
        handle = self._session.current_speech
        slot = self._handles.get(handle.id) if handle is not None else None
        if handle is None or slot is None or slot.reply is None or slot.reply.question_id is None:
            return
        if not self._current(slot.epoch):
            handle.interrupt(force=True)
            return
        question_id = slot.reply.question_id
        self._answerable = question_id  # Capture speech-start identity before another STT turn.
        slot.begun = self._retain(self._begin(question_id, handle))
        slot.begun.add_done_callback(lambda _: slot.settled.set())

    async def _begin(self, question_id: UUID, handle: SpeechHandle) -> bool:
        try:
            begun = await self._interviews.begin_question(self._interview_id, self._run_id, question_id)
        except Exception:
            logger.exception("Could not commit spoken question")
            handle.interrupt(force=True)
            self._retain(self._end_media("voice_dispatch_failed"))
            return False
        if begun:
            self._send_hint()
        else:
            handle.interrupt(force=True)
        return begun

    def _on_speech_done(self, handle: SpeechHandle) -> None:
        if handle.id not in self._observers:
            self._observers[handle.id] = self._retain(self._observe(handle))

    async def _wait_and_observe(self, handle: SpeechHandle) -> None:
        await handle.wait_for_playout()
        self._on_speech_done(handle)

    async def _observe(self, handle: SpeechHandle) -> None:
        slot = self._handles.get(handle.id)
        if slot is None:
            return
        persisted = False
        try:
            if slot.begun is not None:
                await slot.begun
            if slot.reply is None:
                return
            source = "tts_aligned" if slot.aligned and not slot.estimated else "sdk_estimate"
            assistant_items = (
                item for item in handle.chat_items
                if isinstance(item, llm.ChatMessage) and item.role == "assistant"
            )
            observed = False
            for item in assistant_items:
                text = item.text_content or ""
                if not text:
                    continue
                observed = True
                await self._interviews.observe_assistant(
                    self._interview_id, self._run_id, item.id,
                    slot.reply.question_id, slot.reply.kind, text,
                    handle.interrupted or item.interrupted or handle.exception() is not None,
                    source,
                )
                self._send_hint()
            if not observed and slot.reply.question_id is not None and (
                handle.interrupted or handle.exception() is not None
            ):
                # No assistant item means no spoken text, not a fabricated transcript.
                await self._interviews.observe_assistant(
                    self._interview_id, self._run_id, handle.id,
                    slot.reply.question_id, slot.reply.kind, "",
                    handle.interrupted, source,
                )
                self._send_hint()
            if handle.exception() is not None and not self._stopping:
                await self._end_media("tts_unavailable")
            persisted = True
        except Exception:
            logger.exception("Could not persist assistant delivery")
            self._retain(self._end_media("voice_dispatch_failed"))
            raise
        finally:
            slot.settled.set()
            self._handles.pop(handle.id, None)
            if slot.message_id:
                self._reply_by_id.pop(slot.message_id, None)
            if persisted and slot.reply is not None and slot.reply.kind == "closing":
                self._closing_observed = True

    def _on_error(self, event: ErrorEvent) -> None:
        if self._stopping:
            return
        if isinstance(event.error, stt.STTError):
            self._retain(self._end_media("stt_unavailable"))
        elif isinstance(event.error, tts.TTSError):
            self._retain(self._end_media("tts_unavailable"))

    async def _end_media(self, reason: str) -> None:
        if self._stopping:
            return
        self._stopping = True
        self._epoch += 1
        self._silence.set()
        try:
            await self._voice.release_run(self._interview_id, self._run_id, self._job_id, reason)
            await self._flush_hint(self._send_hint())
        finally:
            if self._session is not None:
                await self._session.aclose()

    def _send_hint(self) -> asyncio.Task[None] | None:
        if not self._closed and self._session is not None:
            return self._retain(self._publish_hint())
        return None

    @staticmethod
    async def _flush_hint(task: asyncio.Task[None] | None) -> None:
        if task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (Exception, asyncio.CancelledError):
                logger.exception("Interview state hint could not be flushed before shutdown")

    async def _publish_hint(self) -> None:
        state = await self._interviews.voice_state(self._interview_id, self._run_id)
        if state is None or self._session is None:
            return
        try:
            room = self._session.room_io.room
            if room.isconnected():
                await room.local_participant.publish_data(
                    json.dumps({
                        "type": "interview.state_changed",
                        "interview_id": str(self._interview_id),
                        "revision": state.revision,
                    }),
                    reliable=True, topic="interview.state",
                )
        except Exception:
            # Durable GET is authoritative; a media disconnect cannot roll back a commit.
            logger.exception("Could not publish interview state invalidation")

    def _current(self, epoch: int) -> bool:
        return not self._closed and not self._stopping and self._epoch == epoch and not self._speaking

    def _captured_question(self, message: llm.ChatMessage) -> tuple[UUID | None, int]:
        if not self._captured:
            return None, self._epoch
        started = message.metrics.get("started_speaking_at")
        if isinstance(started, int | float):
            # A VAD false start can have no completed callback. Match the SDK's
            # recorded start time instead of shifting every later question ID.
            closest = min(
                range(len(self._captured)),
                key=lambda index: abs(self._captured[index][0] - started),
            )
            for _ in range(closest):
                self._captured.popleft()
        _, question_id, epoch = self._captured.popleft()
        return question_id, epoch

    async def on_user_turn_completed(
        self, turn_ctx: llm.ChatContext, new_message: llm.ChatMessage,
    ) -> None:
        if new_message.id not in self._seen_turn_ids:
            self._seen_turn_ids.add(new_message.id)
            self._finalized_turns += 1  # Fence recovery before a distinct hook awaits the DB.
        finalized_turn = self._finalized_turns
        captured, epoch = self._captured_question(new_message)
        # A begun question must be durable before a rapid interruption can commit its answer.
        begins = [slot.begun for slot in self._handles.values()
                  if slot.begun is not None and slot.reply is not None
                  and slot.reply.question_id == captured]
        if begins:
            await asyncio.gather(*begins)
        try:
            result = await self._interviews.record_turn(
                self._interview_id, self._run_id, new_message.id,
                captured, new_message.text_content or "",
            )
        except Exception:
            logger.exception("Could not commit finalized candidate turn")
            self._retain(self._end_media("voice_dispatch_failed"))
            raise
        hint = self._send_hint() if result.kind != "duplicate" else None
        enabled = result.kind in {"answer", "repeat"}
        slot = _ReplySlot(message_id=new_message.id, epoch=epoch, enabled=enabled)
        # Agents 1.8.6 creates a speech handle even if llm_node will return None;
        # retain a slot for *every* hook to preserve FIFO association across controls.
        self._reply_queue.append(slot)
        self._reply_by_id[new_message.id] = slot
        if result.kind == "answer":
            self._retain(self._recover_answer(slot, finalized_turn))
        if result.kind == "stop":
            self._retain(self._close_after_stop(hint))

    async def _close_after_stop(self, hint: asyncio.Task[None] | None) -> None:
        self._stopping = True
        self._epoch += 1
        self._silence.set()
        await self._flush_hint(hint)
        if self._session is not None:
            await self._session.aclose()

    async def _recover_answer(self, slot: _ReplySlot, finalized_turn: int) -> None:
        """Retry a saved answer if SDK generation is dropped by a VAD-only turn.

        A listening transition need not lead to a completed-turn hook. Wait for the
        original SDK handle (or its speaking commit) and a quiet grace period before
        taking the planning mutex; the normal llm_node always gets first chance.
        """
        while not self._closed and not self._stopping and self._finalized_turns == finalized_turn:
            try:
                await asyncio.wait_for(slot.settled.wait(), timeout=2)
            except TimeoutError:
                pass
            if slot.begun is not None and slot.begun.done() and slot.begun.result():
                return
            if slot.reply is not None and slot.reply.kind == "closing" and self._closing_scheduled:
                return
            if slot.handle is not None and not slot.handle.done():
                slot.settled.clear()
                continue
            if not await self._wait_for_silence():
                return
            epoch = self._epoch
            await asyncio.sleep(2)  # Let a real second turn's finalized hook win.
            if (not self._current(epoch) or self._finalized_turns != finalized_turn):
                continue
            async with self._planning:
                if (not self._current(epoch) or self._finalized_turns != finalized_turn):
                    continue
                if slot.begun is not None and slot.begun.done() and slot.begun.result():
                    return
                if slot.handle is not None and not slot.handle.done():
                    continue
                # A late SDK llm_node must not also release this answer's reply.
                slot.enabled = False
                try:
                    reply = await self._interviews.prepare_reply(
                        self._interview_id, self._run_id,
                        lambda: self._current(epoch) and self._finalized_turns == finalized_turn,
                    )
                except Exception:
                    logger.exception("Could not recover saved candidate answer")
                    self._retain(self._end_media("voice_dispatch_failed"))
                    return
                self._send_hint()
                if reply is None:
                    await self._stop_if_planning_failed()
                    if self._current(epoch) and self._finalized_turns == finalized_turn:
                        return
                    continue
                if reply.kind == "closing":
                    self._start_evaluation()
                if not self._current(epoch) or self._finalized_turns != finalized_turn:
                    if reply.kind == "closing":
                        self._defer_closing(reply)
                    continue
                if not await self._reply_valid(reply) or not self._current(epoch):
                    return
                if self._finalized_turns != finalized_turn:
                    continue
                self._say_reply(reply, epoch)
                return

    async def on_user_turn_exceeded(self, ev: object) -> None:
        """Never let the SDK generate its default unsanctioned mid-turn reply."""
        await self._wait_for_silence()  # Only a finalized turn may commit an answer.

    async def _stop_if_planning_failed(self) -> None:
        state = await self._interviews.voice_state(self._interview_id, self._run_id)
        if (
            state is not None and state.run_current and state.status == "paused"
            and state.last_error_code in {"llm_unavailable", "llm_invalid_output"}
        ):
            self._retain(self._end_media("disconnect"))

    async def llm_node(
        self, chat_ctx: llm.ChatContext, tools: list[llm.Tool],
        model_settings: ModelSettings,
    ) -> Any:
        user = next((item for item in reversed(chat_ctx.items)
                     if isinstance(item, llm.ChatMessage) and item.role == "user"), None)
        slot = self._reply_by_id.get(user.id) if user is not None else None
        if slot is None or not slot.enabled or not self._current(slot.epoch):
            return None
        async with self._planning:
            if not slot.enabled or not self._current(slot.epoch):
                return None
            try:
                reply = await self._interviews.prepare_reply(
                    self._interview_id, self._run_id,
                    lambda: self._current(slot.epoch),
                )
            except Exception:
                logger.exception("Could not plan candidate reply")
                self._retain(self._end_media("voice_dispatch_failed"))
                return None
            self._send_hint()
            if reply is None:
                await self._stop_if_planning_failed()
                return None
            if reply.kind == "closing":
                self._start_evaluation()
            if not self._current(slot.epoch):
                if reply.kind == "closing":
                    self._defer_closing(reply)
                return None
            if not await self._wait_for_silence():
                return None
            if not self._current(slot.epoch) or slot.handle is None or slot.handle.interrupted:
                if reply.kind == "closing":
                    self._defer_closing(reply)
                return None
            if not await self._reply_valid(reply) or not self._current(slot.epoch):
                return None
            slot.reply = reply
            if reply.kind == "closing":
                self._closing_scheduled = True
                self._closing_handle = slot.handle
            return reply.text

    async def _reply_valid(self, reply: ApprovedReply) -> bool:
        state = await self._interviews.voice_state(self._interview_id, self._run_id)
        if state is None or not state.run_current:
            return False
        if reply.kind == "closing":
            return state.status in {"evaluating", "evaluation_failed", "completed"}
        if state.status not in {"created", "active", "paused"}:
            return False
        return (
            state.current_question_id == reply.question_id
            if reply.kind == "question"
            else state.answerable_question_id == reply.question_id
        )

    def _start_evaluation(self) -> None:
        if not self._evaluation_started:
            self._evaluation_started = True
            self._retain(self._evaluate())

    def _defer_closing(self, reply: ApprovedReply) -> None:
        if not self._closing_scheduled and not self._stopping:
            self._closing_scheduled = True
            self._retain(self._say_closing_when_quiet(reply))

    async def _wait_for_silence(self) -> bool:
        while not self._closed and not self._stopping:
            try:
                await asyncio.wait_for(self._silence.wait(), timeout=2)
                return not self._closed and not self._stopping
            except TimeoutError:
                state = await self._interviews.voice_state(self._interview_id, self._run_id)
                if state is None or not state.run_current:
                    return False
        return False

    async def _say_closing_when_quiet(self, reply: ApprovedReply) -> None:
        while not self._closed and not self._stopping:
            if not await self._wait_for_silence():
                return
            epoch = self._epoch
            if not await self._reply_valid(reply):
                return
            if not self._current(epoch):
                continue
            self._say_reply(reply, epoch)
            return

    def _say_reply(self, reply: ApprovedReply, epoch: int) -> None:
        if self._session is None:
            raise RuntimeError("Bind an AgentSession before speaking")
        # speech_created fires inside say(), not after its SpeechHandle is returned.
        self._say_slot = _ReplySlot(message_id="", epoch=epoch, enabled=True, reply=reply)
        try:
            handle = self._session.say(reply.text)
        finally:
            self._say_slot = None
        if reply.kind == "closing":
            self._closing_scheduled = True
            self._closing_handle = handle
            self._start_evaluation()

    async def _evaluate(self) -> None:
        try:
            await self._interviews.evaluate(self._interview_id)
        except Exception:
            # EvaluationService persists a safe failed state for explicit retry.
            logger.exception("Interview evaluation failed")
        finally:
            self._send_hint()

    async def transcription_node(
        self, text: AsyncIterable[str | TimedString], model_settings: ModelSettings,
    ) -> AsyncIterable[str | TimedString]:
        async for chunk in text:
            if self._session is not None:
                handle = self._session.current_speech
                slot = self._handles.get(handle.id) if handle is not None else None
                if slot is not None:
                    if isinstance(chunk, TimedString):
                        slot.aligned = True
                    else:
                        slot.estimated = True
            yield chunk

    async def start_question(self) -> None:
        """Recover pending assessment or replay the current unanswered question."""
        if self._session is None:
            raise RuntimeError("Bind an AgentSession before starting the interview")
        async with self._planning:
            while not self._closed and not self._stopping:
                if not await self._wait_for_silence():
                    return
                epoch = self._epoch
                state = await self._interviews.voice_state(self._interview_id, self._run_id)
                if state is None or not state.run_current or state.status not in {"created", "active", "paused"}:
                    return
                self._answerable = state.answerable_question_id
                reply = await self._interviews.prepare_reply(
                    self._interview_id, self._run_id,
                    lambda: self._current(epoch),
                )
                self._send_hint()
                if reply is None:
                    await self._stop_if_planning_failed()
                    return
                if reply.kind == "closing":
                    self._start_evaluation()
                if not self._current(epoch):
                    if reply.kind == "closing":
                        self._defer_closing(reply)
                    continue
                if not await self._reply_valid(reply) or not self._current(epoch):
                    return
                self._say_reply(reply, epoch)
                return

    async def interview_finished(self) -> bool:
        """Keep the media run until closing, evaluation and hints are committed."""
        if (
            self._closing_handle is None or not self._closing_handle.done()
            or not self._closing_observed or any(not task.done() for task in self._tasks)
        ):
            return False
        state = await self._interviews.voice_state(self._interview_id, self._run_id)
        return state is not None and state.status in {"evaluating", "evaluation_failed", "completed"}

    async def close(self) -> None:
        self._closed = True
        self._epoch += 1
        self._silence.set()
        if self._session is not None:
            self._session.off("user_state_changed", self._on_user_state)
            self._session.off("agent_state_changed", self._on_agent_state)
            self._session.off("speech_created", self._on_speech_created)
            self._session.off("error", self._on_error)
            self._session.off("close", self._on_session_close)
        for slot in tuple(self._handles.values()):
            if slot.handle is not None:
                if slot.handle.done():
                    self._on_speech_done(slot.handle)
                else:
                    self._retain(self._wait_and_observe(slot.handle))
        while self._tasks:
            settled = tuple(self._tasks)
            await asyncio.gather(*settled, return_exceptions=True)
            # gather can return synchronously for finished tasks, before their
            # scheduled done callbacks remove them from the retained set.
            self._tasks.difference_update(settled)
