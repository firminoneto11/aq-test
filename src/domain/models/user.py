import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from .base import TimeStampedBaseModel


class User(TimeStampedBaseModel):
    __tablename__ = "users"
    __table_args__ = (
        sa.CheckConstraint(
            "email = lower(btrim(email)) AND email <> ''",
            name="ck_users_normalized_email",
        ),
    )

    email: Mapped[str] = mapped_column(sa.String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, default=True, server_default=sa.true()
    )
