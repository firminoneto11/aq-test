from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


InterviewStatus = Literal[
    "created",
    "active",
    "paused",
    "evaluating",
    "completed",
    "evaluation_failed",
    "cancelled",
]
ConnectionState = Literal["provisioning", "ready", "failed"]
QuestionKind = Literal["bank", "follow_up"]
DeliveryStatus = Literal["pending", "completed", "interrupted", "failed"]
TranscriptSpeaker = Literal["user", "assistant"]
TranscriptKind = Literal["input", "question", "answer", "control", "closing"]
ProcessingStatus = Literal["pending", "applied", "not_applicable"]
TextSource = Literal["stt", "tts_aligned", "sdk_estimate"]
DecisionAction = Literal["ask", "finish", "pause"]


class BankQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    text: str = Field(min_length=1, max_length=450)


class PackTopic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    category: Literal["behavioral", "technical"]
    weight: int = Field(gt=0, le=100)
    skill_ids: list[str] = Field(min_length=2, max_length=2)
    questions: list[BankQuestion] = Field(min_length=2, max_length=2)

    @model_validator(mode="after")
    def validate_topic(self) -> "PackTopic":
        if len(set(self.skill_ids)) != len(self.skill_ids):
            raise ValueError("Topic skill IDs must be unique")
        if any(
            not skill_id.startswith(f"{self.id}.")
            or not skill_id.removeprefix(f"{self.id}.").strip()
            for skill_id in self.skill_ids
        ):
            raise ValueError("Skill IDs must be nonempty and prefixed with the topic ID")
        if len({question.id for question in self.questions}) != len(self.questions):
            raise ValueError("Bank question IDs must be unique")
        return self


class RolePack(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(gt=0)
    seniority: Literal["mid"]
    language: Literal["en"]
    topics: list[PackTopic] = Field(min_length=4, max_length=4)

    @model_validator(mode="after")
    def validate_pack(self) -> "RolePack":
        if [topic.category for topic in self.topics] != [
            "behavioral",
            "technical",
            "technical",
            "behavioral",
        ]:
            raise ValueError("Topics must follow behavioral, technical, technical, behavioral order")
        if self.topics[0].id != "ownership":
            raise ValueError("The first topic must be ownership")
        if sum(topic.weight for topic in self.topics) != 100:
            raise ValueError("Topic weights must total 100")
        if len({topic.id for topic in self.topics}) != len(self.topics):
            raise ValueError("Topic IDs must be unique")
        question_ids = [question.id for topic in self.topics for question in topic.questions]
        if len(set(question_ids)) != len(question_ids):
            raise ValueError("Bank question IDs must be unique across topics")
        slug = self.topics[0].questions[0].id.rsplit(".", 2)[0]
        if not slug or any(
            [question.id for question in topic.questions]
            != [f"{slug}.{topic.id}.a", f"{slug}.{topic.id}.b"]
            for topic in self.topics
        ):
            raise ValueError("Bank question IDs must share a job slug and use .<topic-id>.a/b")
        return self


SignalStatus = Literal["unobserved", "insufficient", "assessed"]
EvaluationStatus = Literal["not_ready", "running", "failed", "completed"]


class SignalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    answer_id: UUID
    quote: str = Field(min_length=1)


class RubricSignal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_id: str
    status: SignalStatus
    skill_ids: list[str]
    evidence: list[SignalEvidence]

    @model_validator(mode="after")
    def validate_status(self) -> "RubricSignal":
        if self.status == "assessed" and not self.evidence:
            raise ValueError("An assessed topic requires answer evidence")
        if self.status == "unobserved" and (self.evidence or self.skill_ids):
            raise ValueError("An unobserved topic cannot have evidence or skills")
        if len(self.skill_ids) != len(set(self.skill_ids)):
            raise ValueError("Skill IDs must be unique")
        return self


@dataclass(frozen=True)
class AnsweredQuestion:
    question_id: UUID
    answer_id: UUID
    topic_id: str
    kind: QuestionKind
    answer_text: str
    bank_question_id: str | None


@dataclass(frozen=True)
class InterviewSnapshot:
    pack: RolePack
    answers: tuple[AnsweredQuestion, ...]
    signals: tuple[RubricSignal, ...]
    used_bank_ids: frozenset[str]


@dataclass(frozen=True)
class NextStep:
    action: Literal["ask", "finish"]
    kind: QuestionKind | None
    topic_id: str | None
    source_answer_id: UUID | None
    eligible_bank_ids: tuple[str, ...]
    reason_code: str


class InterviewPolicy:
    """Pure progression over committed answers, validated signals and the frozen pack."""

    def validate_signal(self, signal: RubricSignal, snapshot: InterviewSnapshot) -> None:
        topic = next(
            (topic for topic in snapshot.pack.topics if topic.id == signal.topic_id), None
        )
        if topic is None:
            raise ValueError("Unknown rubric topic")
        if set(signal.skill_ids) - set(topic.skill_ids):
            raise ValueError("Unsupported skill ID")
        answers = {answer.answer_id: answer for answer in snapshot.answers}
        if len(answers) != len(snapshot.answers):
            raise ValueError("Duplicate answer ID")
        for item in signal.evidence:
            answer = answers.get(item.answer_id)
            if answer is None or answer.topic_id != topic.id:
                raise ValueError("Evidence must cite an answer from this topic and interview")
            if item.quote not in answer.answer_text:
                raise ValueError("Evidence quote must be verbatim")
        if signal.skill_ids and not signal.evidence:
            raise ValueError("Demonstrated skills require evidence")
        # The assessor is instructed to emit skill IDs only for positively
        # demonstrated skills. A mention alone is not evidence of competence;
        # free-text semantics cannot be inferred safely from keyword matching.

    def next_step(self, snapshot: InterviewSnapshot) -> NextStep:
        topics = snapshot.pack.topics
        if len(snapshot.signals) != len(topics) or {
            signal.topic_id for signal in snapshot.signals
        } != {topic.id for topic in topics}:
            raise ValueError("A signal is required for every pack topic")
        for signal in snapshot.signals:
            self.validate_signal(signal, snapshot)
        answers = snapshot.answers
        count = len(answers)
        if count > 10:
            raise ValueError("Interview exceeds the ten-question limit")

        def bank(topic_id: str, reason: str) -> NextStep:
            topic = next(topic for topic in topics if topic.id == topic_id)
            eligible = tuple(
                question.id
                for question in topic.questions
                if question.id not in snapshot.used_bank_ids
            )
            if not eligible:
                raise ValueError("No unused bank question for required topic")
            return NextStep("ask", "bank", topic_id, None, eligible, reason)

        def follow_up(answer: AnsweredQuestion, reason: str) -> NextStep:
            return NextStep(
                "ask", "follow_up", answer.topic_id, answer.answer_id, (), reason
            )

        if count == 0:
            return bank(topics[0].id, "initial_topic")
        if count == 1:
            return follow_up(answers[0], "required_follow_up")
        if count == 2:
            return bank(topics[1].id, "required_topic")
        if count == 3:
            return follow_up(answers[2], "required_follow_up")
        if count == 4:
            return bank(topics[2].id, "required_topic")
        if count == 5:
            return bank(topics[3].id, "required_topic")
        if count == 10:
            return NextStep("finish", None, None, None, (), "question_limit")

        signal_by_topic = {signal.topic_id: signal for signal in snapshot.signals}
        gaps = [
            (index, topic)
            for index, topic in enumerate(topics)
            if signal_by_topic[topic.id].status != "assessed"
        ]
        if not gaps:
            return NextStep("finish", None, None, None, (), "coverage_complete")
        # Only answered questions beyond the mandatory first six count as
        # extra probes. A fresh topic wins over another repeated probe.
        probes = {
            topic.id: sum(
                answer.topic_id == topic.id for answer in answers[6:]
            )
            for topic in topics
        }
        _, selected = min(
            gaps,
            key=lambda item: (probes[item[1].id], -item[1].weight, item[0]),
        )
        source = next(
            (answer for answer in reversed(answers) if answer.topic_id == selected.id),
            None,
        )
        if source is None:
            raise ValueError("Gap topic has no answer to follow up")
        return follow_up(source, "evidence_gap")


_STOP_CONTROLS = frozenset(
    {"end the interview", "stop the interview", "cancel the interview"}
)
_REPEAT_CONTROLS = frozenset(
    {
        "repeat", "repeat that", "repeat the question", "clarify the question",
        "can you repeat the question", "could you repeat the question",
        "can you clarify the question", "could you clarify the question",
    }
)


def classify_control(text: str) -> Literal["answer", "repeat", "stop", "empty"]:
    normalized = " ".join(text.lower().split()).rstrip(".?!").strip()
    if not normalized:
        return "empty"
    if normalized.startswith("please "):
        normalized = normalized.removeprefix("please ")
    if normalized in _STOP_CONTROLS:
        return "stop"
    if normalized in _REPEAT_CONTROLS:
        return "repeat"
    return "answer"


class TurnPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    signal: RubricSignal
    bank_question_id: str | None
    follow_up_text: str | None
    source_quote: str | None


class BankChoice(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bank_question_id: str


class JobSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    slug: str
    title: str
    description: str
    pack_version: int


class CurrentQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    ordinal: int
    kind: QuestionKind
    text: str
    topic_id: str
    source_answer_id: UUID | None
    delivery_status: DeliveryStatus


class DecisionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: DecisionAction
    reason_code: str
    reason: str
    topic_id: str | None
    source_answer_id: UUID | None


class SkillDetected(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill_id: str
    evidence: list[SignalEvidence]


class InterviewState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    job: JobSummary
    status: InterviewStatus
    revision: int
    question_count: int
    answered_count: int
    follow_up_count: int
    current_question: CurrentQuestion | None
    answerable_question_id: UUID | None
    rubric: list[RubricSignal]
    topics_covered: list[str]
    gaps: list[str]
    skills_detected: list[SkillDetected]
    decision: DecisionSummary | None
    evaluation_status: EvaluationStatus
    last_error_code: str | None
    can_resume: bool


class TranscriptAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    text: str


class TranscriptQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    ordinal: int
    kind: QuestionKind
    text: str
    topic_id: str
    source_answer_id: UUID | None
    source_quote: str | None
    answer: TranscriptAnswer | None


class TranscriptMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: UUID
    sequence: int
    run_id: UUID
    sdk_message_id: str
    speaker: TranscriptSpeaker
    question_id: UUID | None
    answer_id: UUID | None
    kind: TranscriptKind
    processing_status: ProcessingStatus
    text: str
    interrupted: bool
    text_source: TextSource


class TranscriptResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interview_id: UUID
    status: InterviewStatus
    questions: list[TranscriptQuestion]
    messages: list[TranscriptMessage]


class EvaluationClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    answer_ids: list[UUID]


class RubricScore(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic_id: str
    score: int = Field(ge=0, le=4)
    answer_ids: list[UUID]


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strengths: list[EvaluationClaim]
    concerns: list[EvaluationClaim]
    rubric_scores: list[RubricScore]
    overall_score: int = Field(ge=0, le=100)
    insufficient_evidence: bool
    gaps: list[str]


class EvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: EvaluationStatus
    result: Evaluation | None
    error_code: str | None
    can_retry: bool
