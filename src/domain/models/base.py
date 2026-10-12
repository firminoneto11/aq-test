from datetime import datetime
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy import event
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    ORMExecuteState,
    Session,
    mapped_column,
    with_loader_criteria,
)
from uuid_extensions import uuid7

from shared.utils import datetime_utc


class TimeStampedBaseModel(DeclarativeBase):
    __abstract__ = True

    id: Mapped[UUID] = mapped_column(
        sa.UUID,
        primary_key=True,
        default=uuid7,  # type: ignore
        sort_order=-1,
    )

    # NOTE: Subclasses fields will be placed in-between these base fields on a database
    # level.

    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), default=datetime_utc, sort_order=9997
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        default=datetime_utc,
        onupdate=datetime_utc,
        sort_order=9998,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        sa.DateTime(timezone=True), default=None, sort_order=9999
    )

    def soft_delete(self):
        self.deleted_at = datetime_utc(unix=False)

    def restore(self):
        self.deleted_at = None


@event.listens_for(Session, "do_orm_execute")
def _apply_soft_delete_filter(execute_state: ORMExecuteState):  # pyright: ignore[reportUnusedFunction]
    should_include_deleted = execute_state.execution_options.get(
        "include_deleted", False
    )

    if execute_state.is_select and (not should_include_deleted):
        execute_state.statement = execute_state.statement.options(
            with_loader_criteria(
                TimeStampedBaseModel,
                lambda cls: cls.deleted_at.is_(None),
                include_aliases=True,
            )
        )
