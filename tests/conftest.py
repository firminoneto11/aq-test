from functools import partial
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest
from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient
from testcontainers.community.postgres import PostgresContainer
from uvloop import new_event_loop

from conf import settings
from domain.models import TimeStampedBaseModel
from entrypoints.api.http.fastapi_.factory import ASGIFactory
from entrypoints.api.http.fastapi_.utils import reverse_url as reverse
from entrypoints.api.http.fastapi_.utils.state import use_db_session

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.ports import SqlDBClientPort
    from entrypoints.api.http.fastapi_.types_ import AsgiApp


def pytest_asyncio_loop_factories(config: Any, item: Any):
    return {"stdlib": new_event_loop}


@pytest.fixture(scope="session", autouse=True)
def postgres_container():
    with PostgresContainer("postgres:18") as postgres:
        settings.DB_SERVER = postgres.get_connection_url(driver=settings.DB_DRIVER)
        yield postgres


@pytest.fixture(scope="session")
async def asgi_config(postgres_container: PostgresContainer):
    config, main_app = ASGIFactory.new(base_model_to_migrate=TimeStampedBaseModel)
    async with LifespanManager(main_app):
        yield config, main_app


@pytest.fixture(scope="session")
def reverse_url(asgi_config: tuple[ASGIFactory, AsgiApp]):
    _, main_app = asgi_config
    return partial(reverse, app=main_app)


@pytest.fixture(scope="session")
def db(asgi_config: tuple[ASGIFactory, AsgiApp]):
    _, main_app = asgi_config
    yield main_app.state.db


@pytest.fixture
async def db_session(db: SqlDBClientPort):
    async with db.begin_session() as session:
        with patch.object(target=session, attribute="commit", new=session.flush):
            try:
                yield session
            finally:
                await session.rollback()


@pytest.fixture
async def http_client(
    asgi_config: tuple[ASGIFactory, AsgiApp], db_session: AsyncSession
):
    config, main_app = asgi_config

    for app in config.apps:
        app.dependency_overrides[use_db_session] = lambda: db_session

    transport = ASGITransport(app=main_app)
    async with AsyncClient(base_url="http://test", transport=transport) as client:
        yield client

    for app in config.apps:
        app.dependency_overrides.clear()
