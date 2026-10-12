from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from app.ports import SqlDBClientPort

from .sqlalchemy_ import SqlAlchemyBackend

if TYPE_CHECKING:
    from sqlalchemy.orm import DeclarativeBase


class SqlDBClientAdapter(SqlDBClientPort):
    def __init__(self, connection_string: str, autocommit: bool):
        self._database = SqlAlchemyBackend(connection_string, autocommit)

    @property
    def engine(self):
        return self.database.engine

    @property
    def database(self):
        return self._database

    async def connect(self, **kwargs: Any):
        await self.database.connect(**kwargs)

    async def disconnect(self):
        await self.database.disconnect()

    @asynccontextmanager
    async def begin_session(self):
        async with self.database.begin_session() as session:
            yield session

    async def ping(self):
        await self.database.ping()

    async def migrate(self, base_model: type[DeclarativeBase], drop: bool = False):
        await self.database.migrate(base_model=base_model, drop=drop)
