from typing import TYPE_CHECKING, Protocol

from fastapi import FastAPI

if TYPE_CHECKING:
    from app.ports import ErrorTrackingPort, SqlDBClientPort


class _ApplicationMount(Protocol):
    path: str
    app: AsgiApp
    name: str


class _CustomAppState:
    mounted_applications: list[_ApplicationMount]
    db: SqlDBClientPort
    error_tracker: ErrorTrackingPort


class AsgiApp(FastAPI):
    state: _CustomAppState  # pyright: ignore[reportIncompatibleVariableOverride]
