import asyncio
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool, StaticPool

if TYPE_CHECKING:
    from sqlalchemy.engine import AdaptedConnection
    from sqlalchemy.ext.asyncio import AsyncEngine
    from sqlalchemy.orm import DeclarativeBase

    class _AsyncCursor(Protocol):
        async def execute(self, sql: str) -> Any: ...
        async def close(self) -> None: ...

    class _AsyncConnection(Protocol):
        async def cursor(self) -> _AsyncCursor: ...


class _SoftDeleteSession(Session):
    def delete(self, instance: Any):
        if (soft_delete := getattr(instance, "soft_delete", None)) and (
            callable(soft_delete)
        ):
            soft_delete()
        else:
            super().delete(instance)


class _SoftDeleteAsyncSession(AsyncSession):
    sync_session_class = _SoftDeleteSession


class SqlAlchemyBackend:
    _connection_string: str
    _autocommit: bool

    _engine: AsyncEngine | None
    _start_db_session: async_sessionmaker[AsyncSession] | None
    _active_sessions: set[AsyncSession]
    _is_connected: bool
    _lock: asyncio.Lock

    def __init__(self, connection_string: str, autocommit: bool):
        self._connection_string = connection_string
        self._autocommit = autocommit
        self._lock = asyncio.Lock()
        self._set_defaults()

    @property
    def engine(self):
        if self._engine is None:
            raise ValueError("'_engine' is None. Can not proceed")
        return self._engine

    @property
    def start_db_session(self):
        if self._start_db_session is None:
            raise ValueError("'_start_db_session' is None. Can not proceed")
        return self._start_db_session

    @property
    def active_sessions(self):
        return self._active_sessions

    @property
    def using_sqlite(self):
        return self._connection_string.startswith("sqlite")

    @property
    def is_sqlite_memory_based(self):
        return self.using_sqlite and (":memory:" in self._connection_string)

    @asynccontextmanager
    async def _acquire_lock(self):
        await self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    async def connect(self, **kwargs: Any):
        async with self._acquire_lock():
            if self._is_connected:
                return

            echo_sql = kwargs.pop("echo_sql", False)

            kws: dict[str, Any] = {}
            if self.using_sqlite:
                kws["connect_args"] = {"check_same_thread": False}
                kws["poolclass"] = (
                    StaticPool if self.is_sqlite_memory_based else NullPool
                )
            else:
                kws["pool_pre_ping"] = kwargs.pop("pool_pre_ping", True)
                kws["pool_recycle"] = kwargs.pop("pool_recycle", 3600)
                kws.update(kwargs)

            self._engine = create_async_engine(
                url=self._connection_string, echo=echo_sql, **kws
            )

            if self.using_sqlite:

                @event.listens_for(self._engine.sync_engine, "connect")
                def _set_sqlite_pragma(dbapi_conn: AdaptedConnection, _: Any):  # pyright: ignore[reportUnusedFunction]
                    async def _enable_foreign_keys(connection: _AsyncConnection):
                        cursor = await connection.cursor()
                        await cursor.execute("PRAGMA foreign_keys=ON")
                        await cursor.close()

                    dbapi_conn.run_async(_enable_foreign_keys)

            self._start_db_session = async_sessionmaker(
                self.engine,
                class_=_SoftDeleteAsyncSession,
                autoflush=False,
                autocommit=False,
                expire_on_commit=False,
            )

            await self._check_connection()

    async def disconnect(self):
        async with self._acquire_lock():
            await self._disconnect()

    async def _disconnect(self):
        if not self._is_connected:
            return

        try:
            results = await asyncio.gather(
                *[s.close() for s in self.active_sessions],
                return_exceptions=True,
            )
            if errors := [r for r in results if isinstance(r, Exception)]:
                raise ExceptionGroup("Failed to close one or more sessions", errors)
        finally:
            await self.engine.dispose()
            self._set_defaults()

    @asynccontextmanager
    async def begin_session(self):
        self._validate_connection()
        async with self.start_db_session() as session:
            self.active_sessions.add(session)
            try:
                yield session
            except Exception:
                await session.rollback()
                raise
            else:
                if self._autocommit:
                    await session.commit()
            finally:
                self.active_sessions.discard(session)

    async def ping(self):
        async with self.begin_session() as session:
            await session.execute(text("SELECT 1;"))

    async def migrate(self, base_model: type[DeclarativeBase], drop: bool = False):
        self._validate_connection()

        async with self.engine.begin() as conn:
            if drop:
                await conn.run_sync(base_model.metadata.drop_all)
            await conn.run_sync(base_model.metadata.create_all)

    def _validate_connection(self):
        if not self._is_connected:
            raise ConnectionError("This instance is not connected to the database yet")

    async def _check_connection(self):
        try:
            self._is_connected = True
            await self.ping()
        except Exception as exc:
            await self._disconnect()
            raise ConnectionError("Failed to connect to the database") from exc

    def _set_defaults(self):
        self._engine = None
        self._start_db_session = None
        self._active_sessions = set()
        self._is_connected = False
