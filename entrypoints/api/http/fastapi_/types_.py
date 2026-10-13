from typing import TYPE_CHECKING, Protocol

from fastapi import FastAPI

if TYPE_CHECKING:
    from app.ports import ErrorTrackingPort, PasswordPort, SqlDBClientPort
    from app.usecases.interviews import InterviewService
    from app.usecases.voice_sessions import VoiceSessionService
    from infra.voice import LiveKitGateway


class _ApplicationMount(Protocol):
    path: str
    app: AsgiApp
    name: str


class _CustomAppState:
    mounted_applications: list[_ApplicationMount]
    db: SqlDBClientPort
    error_tracker: ErrorTrackingPort
    passwords: PasswordPort
    interviews: InterviewService
    voice_gateway: LiveKitGateway | None
    voice_sessions: VoiceSessionService


class AsgiApp(FastAPI):
    state: _CustomAppState  # pyright: ignore[reportIncompatibleVariableOverride]
