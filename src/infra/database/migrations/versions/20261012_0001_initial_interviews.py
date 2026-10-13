"""Initial durable accounts, role packs, interviews and transcript records.

Revision ID: 20261012_0001
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import SchemaItem

revision: str = "20261012_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _identity_and_timestamps() -> tuple[SchemaItem, ...]:
    return (
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
        *_identity_and_timestamps(),
        sa.UniqueConstraint("email"),
        sa.CheckConstraint(
            "email = lower(btrim(email)) AND email <> ''",
            name="ck_users_normalized_email",
        ),
    )
    op.create_table(
        "jobs",
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("pack_version", sa.Integer(), nullable=False),
        sa.Column("pack", postgresql.JSONB(), nullable=False),
        *_identity_and_timestamps(),
        sa.UniqueConstraint("slug"),
        sa.CheckConstraint("pack_version > 0", name="ck_jobs_pack_version_positive"),
        sa.CheckConstraint("jsonb_typeof(pack) = 'object'", name="ck_jobs_pack_object"),
    )
    op.create_table(
        "interviews",
        sa.Column("user_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=False),
        sa.Column("job_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status", sa.String(length=24), server_default="created", nullable=False
        ),
        sa.Column("revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("current_question_id", sa.UUID(), nullable=True),
        sa.Column("answerable_question_id", sa.UUID(), nullable=True),
        sa.Column(
            "signals",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("finish_reason", sa.String(length=64), nullable=True),
        sa.Column("evaluation", postgresql.JSONB(), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("evaluation_claim_id", sa.UUID(), nullable=True),
        sa.Column(
            "evaluation_claim_expires_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("worker_job_id", sa.String(length=255), nullable=True),
        sa.Column("room_name", sa.String(length=255), nullable=True),
        sa.Column("connect_request_id", sa.UUID(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "connection_state",
            sa.String(length=16),
            server_default="failed",
            nullable=False,
        ),
        sa.Column("grant_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("grant_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("create_request_id", sa.UUID(), nullable=False),
        *_identity_and_timestamps(),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"]),
        sa.UniqueConstraint(
            "user_id", "create_request_id", name="uq_interviews_user_create_request"
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
    op.create_table(
        "interview_questions",
        sa.Column("interview_id", sa.UUID(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("topic_id", sa.String(length=100), nullable=False),
        sa.Column("bank_question_id", sa.String(length=255), nullable=True),
        sa.Column("source_answer_id", sa.UUID(), nullable=True),
        sa.Column("source_quote", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "delivery_status",
            sa.String(length=16),
            server_default="pending",
            nullable=False,
        ),
        *_identity_and_timestamps(),
        sa.ForeignKeyConstraint(["interview_id"], ["interviews.id"]),
        sa.UniqueConstraint(
            "interview_id", "bank_question_id", name="uq_interview_questions_bank_id"
        ),
        sa.UniqueConstraint(
            "interview_id", "ordinal", name="uq_interview_questions_ordinal"
        ),
        sa.CheckConstraint(
            "ordinal BETWEEN 1 AND 10", name="ck_interview_questions_ordinal"
        ),
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
    op.create_table(
        "interview_answers",
        sa.Column("question_id", sa.UUID(), nullable=False),
        sa.Column("interview_id", sa.UUID(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        *_identity_and_timestamps(),
        sa.ForeignKeyConstraint(["question_id"], ["interview_questions.id"]),
        sa.ForeignKeyConstraint(["interview_id"], ["interviews.id"]),
        sa.UniqueConstraint("question_id", name="uq_interview_answers_question_id"),
        sa.CheckConstraint(
            "revision >= 1", name="ck_interview_answers_revision_positive"
        ),
    )
    # Both pending question pointers and follow-up source references are circular.
    # Create them only after both sides' tables exist.
    op.create_foreign_key(
        "fk_interviews_current_question_id_interview_questions",
        "interviews",
        "interview_questions",
        ["current_question_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_interviews_answerable_question_id_interview_questions",
        "interviews",
        "interview_questions",
        ["answerable_question_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_interview_questions_source_answer_id_interview_answers",
        "interview_questions",
        "interview_answers",
        ["source_answer_id"],
        ["id"],
    )
    op.create_table(
        "transcript_entries",
        sa.Column("interview_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("sdk_message_id", sa.Text(), nullable=False),
        sa.Column("speaker", sa.String(length=16), nullable=False),
        sa.Column("question_id", sa.UUID(), nullable=True),
        sa.Column("answer_id", sa.UUID(), nullable=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("processing_status", sa.String(length=20), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column(
            "interrupted", sa.Boolean(), server_default=sa.false(), nullable=False
        ),
        sa.Column("text_source", sa.String(length=16), nullable=False),
        *_identity_and_timestamps(),
        sa.ForeignKeyConstraint(["interview_id"], ["interviews.id"]),
        sa.ForeignKeyConstraint(["question_id"], ["interview_questions.id"]),
        sa.ForeignKeyConstraint(["answer_id"], ["interview_answers.id"]),
        sa.UniqueConstraint(
            "interview_id", "sequence", name="uq_transcript_entries_sequence"
        ),
        sa.UniqueConstraint(
            "interview_id",
            "run_id",
            "sdk_message_id",
            name="uq_transcript_entries_sdk_message",
        ),
        sa.CheckConstraint(
            "sequence >= 1", name="ck_transcript_entries_sequence_positive"
        ),
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
    op.create_table(
        "interview_decisions",
        sa.Column("interview_id", sa.UUID(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("question_id", sa.UUID(), nullable=True),
        sa.Column("topic_id", sa.String(length=100), nullable=True),
        sa.Column("source_answer_id", sa.UUID(), nullable=True),
        sa.Column("reason_code", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("signals", postgresql.JSONB(), nullable=False),
        *_identity_and_timestamps(),
        sa.ForeignKeyConstraint(["interview_id"], ["interviews.id"]),
        sa.ForeignKeyConstraint(["question_id"], ["interview_questions.id"]),
        sa.ForeignKeyConstraint(["source_answer_id"], ["interview_answers.id"]),
        sa.UniqueConstraint(
            "interview_id", "revision", name="uq_interview_decisions_revision"
        ),
        sa.CheckConstraint(
            "revision >= 0", name="ck_interview_decisions_revision_nonnegative"
        ),
        sa.CheckConstraint(
            "action IN ('ask', 'finish', 'pause')", name="ck_interview_decisions_action"
        ),
    )


def downgrade() -> None:
    op.drop_table("interview_decisions")
    op.drop_table("transcript_entries")
    op.drop_constraint(
        "fk_interview_questions_source_answer_id_interview_answers",
        "interview_questions",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_interviews_answerable_question_id_interview_questions",
        "interviews",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_interviews_current_question_id_interview_questions",
        "interviews",
        type_="foreignkey",
    )
    op.drop_table("interview_answers")
    op.drop_table("interview_questions")
    op.drop_table("interviews")
    op.drop_table("jobs")
    op.drop_table("users")
