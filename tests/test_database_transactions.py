from pathlib import Path
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError

from app.ports import SqlDBClientPort
from conf import settings
from domain.models import User
from infra.database.adapter import SqlDBClientAdapter
from infra.passwords import PasswordAdapter


@pytest.mark.parametrize(
    "scheme",
    ("postgres://", "postgresql://", "postgresql+driver://", "postgresql+asyncpg://"),
)
async def test_fly_and_local_database_urls_reach_postgres_schema(
    db: SqlDBClientPort, scheme: str
) -> None:
    # The session-scoped db fixture creates the schema in this Postgres instance.
    address = settings.DB_SERVER.partition("://")[2]
    config = settings.model_copy(
        update={"DB_SERVER": f"{scheme}{address}", "DB_DRIVER": "asyncpg"}
    )
    connection = SqlDBClientAdapter(config.DATABASE_URL, autocommit=True)
    await connection.connect()
    try:
        async with connection.begin_session() as session:
            count = await session.scalar(sa.select(sa.func.count()).select_from(User))
            assert count is not None and count >= 0
    finally:
        await connection.disconnect()


async def test_rollback_commit_and_soft_delete_are_visible_across_connections(
    db: SqlDBClientPort,
):
    other = SqlDBClientAdapter(settings.DATABASE_URL, autocommit=True)
    await other.connect()
    email = f"transaction-{uuid4()}@example.com"
    password_hash = await PasswordAdapter().hash_password(
        "correct-horse-battery-staple"
    )
    persisted_id = None
    try:
        with pytest.raises(RuntimeError):
            async with db.begin_session() as session:
                session.add(User(email=email, password_hash=password_hash))
                await session.flush()
                raise RuntimeError("rollback this incomplete signup")

        async with other.begin_session() as session:
            assert (
                await session.scalar(sa.select(User).where(User.email == email)) is None
            )

        async with db.begin_session() as session:
            user = User(email=email, password_hash=password_hash)
            session.add(user)
            await session.flush()
            persisted_id = user.id

        async with other.begin_session() as session:
            saved = await session.scalar(sa.select(User).where(User.id == persisted_id))
            assert saved is not None and saved.email == email

        async with db.begin_session() as session:
            user = await session.get(User, persisted_id)
            assert user is not None
            await session.delete(user)

        async with other.begin_session() as session:
            assert (
                await session.scalar(sa.select(User).where(User.id == persisted_id))
                is None
            )
            deleted = await session.scalar(
                sa.select(User)
                .where(User.id == persisted_id)
                .execution_options(include_deleted=True)
            )
            assert deleted is not None and deleted.deleted_at is not None
    finally:
        if persisted_id is not None:
            async with db.begin_session() as session:
                await session.execute(sa.delete(User).where(User.id == persisted_id))
        await other.disconnect()


async def test_sqlite_memory_sessions_share_tables_and_enforce_foreign_keys() -> None:
    local = SqlDBClientAdapter("sqlite+aiosqlite:///:memory:", autocommit=True)
    await local.connect()
    try:
        await local.connect()  # Reconnecting must not reset an in-memory database.
        async with local.begin_session() as session:
            await session.execute(
                sa.text("CREATE TABLE owners (id INTEGER PRIMARY KEY)")
            )
            await session.execute(
                sa.text(
                    "CREATE TABLE records (id INTEGER PRIMARY KEY, owner_id INTEGER "
                    "NOT NULL REFERENCES owners(id))"
                )
            )

        with pytest.raises(IntegrityError):
            async with local.begin_session() as session:
                await session.execute(
                    sa.text("INSERT INTO records (id, owner_id) VALUES (1, 404)")
                )

        async with local.begin_session() as session:
            await session.execute(sa.text("INSERT INTO owners (id) VALUES (9)"))
            await session.execute(
                sa.text("INSERT INTO records (id, owner_id) VALUES (1, 9)")
            )
        async with local.begin_session() as session:
            assert await session.scalar(sa.text("SELECT owner_id FROM records")) == 9
    finally:
        await local.disconnect()
    await local.disconnect()
    with pytest.raises(ConnectionError):
        await local.ping()


async def test_failed_connection_cannot_leak_a_live_database_engine(
    tmp_path: Path,
) -> None:
    missing_parent = tmp_path / "not-created" / "test.sqlite3"
    local = SqlDBClientAdapter(f"sqlite+aiosqlite:///{missing_parent}", autocommit=True)
    with pytest.raises(ConnectionError):
        await local.connect()
    with pytest.raises(ValueError, match="None"):
        _ = local.engine
    await local.disconnect()
