"""Guard service access across concurrent ASGI and worker task contexts."""

import asyncio
from contextvars import Context
from typing import TYPE_CHECKING

import pytest

from app.ports import SqlDBClientPort
from conf import settings
from infra.database.adapter import SqlDBClientAdapter
from infra.error_tracking.adapter import ErrorTrackingAdapter
from shared import context

if TYPE_CHECKING:
    from entrypoints.api.http.fastapi_.factory import ASGIFactory
    from entrypoints.api.http.fastapi_.types_ import AsgiApp


async def test_binding_once_and_new_tasks_reuse_services_without_inheritance(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, app = asgi_config
    global_tracker = app.state.error_tracker
    shadow_db = SqlDBClientAdapter(settings.DATABASE_URL, autocommit=True)
    shadow_tracker = ErrorTrackingAdapter("console")
    monkeypatch.setattr(context, "_bound", False)
    monkeypatch.setattr(context, "_db_global", None)
    monkeypatch.setattr(context, "_error_tracker_global", None)

    async def access_services() -> tuple[SqlDBClientPort, object]:
        await asyncio.sleep(0)
        return context.get_db(), context.get_error_tracker()

    with pytest.raises(RuntimeError, match="db not bound"):
        Context().run(context.get_db)
    with pytest.raises(RuntimeError, match="error_tracker not bound"):
        Context().run(context.get_error_tracker)

    context.bind(db=db, error_tracker=global_tracker)
    with pytest.raises(RuntimeError, match="already bound"):
        context.bind(db=shadow_db, error_tracker=shadow_tracker)

    # A fresh task inherits no ContextVars, but receives the app-wide services.
    assert await Context().run(asyncio.create_task, access_services()) == (
        db,
        global_tracker,
    )
    assert context.get_db() is db
    assert context.get_error_tracker() is global_tracker
