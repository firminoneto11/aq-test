from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, validates

from domain.interviews import (
    ConnectionState,
    DecisionAction,
    DeliveryStatus,
    InterviewStatus,
    ProcessingStatus,
    QuestionKind,
    RolePack,
    TextSource,
    TranscriptKind,
    TranscriptSpeaker,
)

from .base import TimeStampedBaseModel


class Job(TimeStampedBaseModel):
    __tablename__ = "jobs"
    __table_args__ = (
        sa.CheckConstraint("pack_version > 0", name="ck_jobs_pack_version_positive"),
        sa.CheckConstraint("jsonb_typeof(pack) = 'object'", name="ck_jobs_pack_object"),
    )

    slug: Mapped[str] = mapped_column(sa.String(100), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(sa.String(200), nullable=False)
    description: Mapped[str] = mapped_column(sa.Text, nullable=False)
    pack_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    pack: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)

    @validates("pack")
    def validate_pack(self, _key: str, value: RolePack | dict[str, object]) -> dict[str, object]:
        return RolePack.model_validate(value).model_dump(mode="json")


class Interview(TimeStampedBaseModel):
    __tablename__ = "interviews"
    __table_args__ = (
        sa.UniqueConstraint("user_id", "create_request_id", name="uq_interviews_user_create_request"),
        sa.ForeignKeyConstraint(
            ["current_question_id"],
            ["interview_questions.id"],
            name="fk_interviews_current_question_id_interview_questions",
            use_alter=True,
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["answerable_question_id"],
            ["interview_questions.id"],
            name="fk_interviews_answerable_question_id_interview_questions",
            use_alter=True,
            ondelete="SET NULL",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_interviews_revision_nonnegative"),
        sa.CheckConstraint(
            "status IN ('created', 'active', 'paused', 'evaluating', 'completed', 'evaluation_failed', 'cancelled')",
            name="ck_interviews_status",
        ),
        sa.CheckConstraint(
            "connection_state IN ('provisioning', 'ready', 'failed')",
            name="ck_interviews_connection_state",
        ),
    )

    user_id: Mapped[UUID] = mapped_column(sa.ForeignKey("users.id"), nullable=False)
    job_id: Mapped[UUID] = mapped_column(sa.ForeignKey("jobs.id"), nullable=False)
    job_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    status: Mapped[InterviewStatus] = mapped_column(
        sa.String(24), nullable=False, default="created", server_default="created"
    )
    revision: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default="0"
    )
    current_question_id: Mapped[UUID | None] = mapped_column(sa.UUID, nullable=True)
    answerable_question_id: Mapped[UUID | None] = mapped_column(sa.UUID, nullable=True)
    signals: Mapped[list[dict[str, object]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    finish_reason: Mapped[str | None] = mapped_column(sa.String(64))
    evaluation: Mapped[dict[str, object] | None] = mapped_column(JSONB)
    last_error_code: Mapped[str | None] = mapped_column(sa.String(64))
    evaluation_claim_id: Mapped[UUID | None] = mapped_column(sa.UUID)
    evaluation_claim_expires_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True)
    )
    run_id: Mapped[UUID | None] = mapped_column(sa.UUID)
    worker_job_id: Mapped[str | None] = mapped_column(sa.String(255))
    room_name: Mapped[str | None] = mapped_column(sa.String(255))
    connect_request_id: Mapped[UUID | None] = mapped_column(sa.UUID)
    lease_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    connection_state: Mapped[ConnectionState] = mapped_column(
        sa.String(16), nullable=False, default="failed", server_default="failed"
    )
    grant_issued_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    grant_expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    create_request_id: Mapped[UUID] = mapped_column(sa.UUID, nullable=False)


class InterviewQuestion(TimeStampedBaseModel):
    __tablename__ = "interview_questions"
    __table_args__ = (
        sa.UniqueConstraint(
            "interview_id", "bank_question_id", name="uq_interview_questions_bank_id"
        ),
        sa.UniqueConstraint("interview_id", "ordinal", name="uq_interview_questions_ordinal"),
        sa.ForeignKeyConstraint(
            ["source_answer_id"],
            ["interview_answers.id"],
            name="fk_interview_questions_source_answer_id_interview_answers",
            use_alter=True,
        ),
        sa.CheckConstraint("ordinal BETWEEN 1 AND 10", name="ck_interview_questions_ordinal"),
        sa.CheckConstraint(
            "kind IN ('bank', 'follow_up')", name="ck_interview_questions_kind"
        ),
        sa.CheckConstraint(
            "delivery_status IN ('pending', 'completed', 'interrupted', 'failed')",
            name="ck_interview_questions_delivery_status",
        ),
        sa.CheckConstraint(
            "(kind = 'bank' AND bank_question_id IS NOT NULL AND source_answer_id IS NULL AND source_quote IS NULL) "
            "OR (kind = 'follow_up' AND bank_question_id IS NULL AND source_answer_id IS NOT NULL AND source_quote IS NOT NULL)",
            name="ck_interview_questions_source_kind",
        ),
        sa.CheckConstraint(
            "length(text) BETWEEN 1 AND 450",
            name="ck_interview_questions_text_length",
        ),
        sa.CheckConstraint(
            "source_quote IS NULL OR length(btrim(source_quote)) > 0",
            name="ck_interview_questions_nonempty_source_quote",
        ),
    )

    interview_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("interviews.id"), nullable=False
    )
    ordinal: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    kind: Mapped[QuestionKind] = mapped_column(sa.String(16), nullable=False)
    topic_id: Mapped[str] = mapped_column(sa.String(100), nullable=False)
    bank_question_id: Mapped[str | None] = mapped_column(sa.String(255))
    source_answer_id: Mapped[UUID | None] = mapped_column(sa.UUID)
    source_quote: Mapped[str | None] = mapped_column(sa.Text)
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    delivery_status: Mapped[DeliveryStatus] = mapped_column(
        sa.String(16), nullable=False, default="pending", server_default="pending"
    )


class InterviewAnswer(TimeStampedBaseModel):
    __tablename__ = "interview_answers"
    __table_args__ = (
        sa.UniqueConstraint("question_id", name="uq_interview_answers_question_id"),
        sa.CheckConstraint("revision >= 1", name="ck_interview_answers_revision_positive"),
    )

    question_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("interview_questions.id"), nullable=False
    )
    interview_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("interviews.id"), nullable=False
    )
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    revision: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=1, server_default="1"
    )


class TranscriptEntry(TimeStampedBaseModel):
    __tablename__ = "transcript_entries"
    __table_args__ = (
        sa.UniqueConstraint("interview_id", "sequence", name="uq_transcript_entries_sequence"),
        sa.UniqueConstraint(
            "interview_id", "run_id", "sdk_message_id", name="uq_transcript_entries_sdk_message"
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_transcript_entries_sequence_positive"),
        sa.CheckConstraint(
            "speaker IN ('user', 'assistant')", name="ck_transcript_entries_speaker"
        ),
        sa.CheckConstraint(
            "kind IN ('input', 'question', 'answer', 'control', 'closing')",
            name="ck_transcript_entries_kind",
        ),
        sa.CheckConstraint(
            "processing_status IN ('pending', 'applied', 'not_applicable')",
            name="ck_transcript_entries_processing_status",
        ),
        sa.CheckConstraint(
            "text_source IN ('stt', 'tts_aligned', 'sdk_estimate')",
            name="ck_transcript_entries_text_source",
        ),
    )

    interview_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("interviews.id"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    run_id: Mapped[UUID] = mapped_column(sa.UUID, nullable=False)
    sdk_message_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    speaker: Mapped[TranscriptSpeaker] = mapped_column(sa.String(16), nullable=False)
    question_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("interview_questions.id")
    )
    answer_id: Mapped[UUID | None] = mapped_column(sa.ForeignKey("interview_answers.id"))
    kind: Mapped[TranscriptKind] = mapped_column(sa.String(16), nullable=False)
    processing_status: Mapped[ProcessingStatus] = mapped_column(
        sa.String(20), nullable=False
    )
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    interrupted: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=False, server_default=sa.false()
    )
    text_source: Mapped[TextSource] = mapped_column(sa.String(16), nullable=False)


class InterviewDecision(TimeStampedBaseModel):
    __tablename__ = "interview_decisions"
    __table_args__ = (
        sa.UniqueConstraint("interview_id", "revision", name="uq_interview_decisions_revision"),
        sa.CheckConstraint("revision >= 0", name="ck_interview_decisions_revision_nonnegative"),
        sa.CheckConstraint(
            "action IN ('ask', 'finish', 'pause')", name="ck_interview_decisions_action"
        ),
    )

    interview_id: Mapped[UUID] = mapped_column(
        sa.ForeignKey("interviews.id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    action: Mapped[DecisionAction] = mapped_column(sa.String(16), nullable=False)
    question_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("interview_questions.id")
    )
    topic_id: Mapped[str | None] = mapped_column(sa.String(100))
    source_answer_id: Mapped[UUID | None] = mapped_column(
        sa.ForeignKey("interview_answers.id")
    )
    reason_code: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    signals: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False)
