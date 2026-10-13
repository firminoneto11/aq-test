from typing import Annotated, cast

from fastapi import Depends
from fastapi.requests import Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.ports import ErrorTrackingPort, PasswordPort, SqlDBClientPort
from app.usecases.auth import AuthUseCase
from app.usecases.interviews import InterviewService
from app.usecases.voice_sessions import VoiceSessionService
from conf import settings
from domain.schemas import UserSchema
from entrypoints.api.http.fastapi_.types_ import AsgiApp

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/token")


def use_db_client(req: Request):
    return cast(AsgiApp, req.app).state.db


def use_passwords(req: Request):
    return cast(AsgiApp, req.app).state.passwords


DBClientDep = Annotated[SqlDBClientPort, Depends(use_db_client)]
PasswordDep = Annotated[PasswordPort, Depends(use_passwords)]


async def use_auth(
    token: Annotated[str, Depends(_oauth2_scheme)],
    db_client: DBClientDep,
    passwords: PasswordDep,
):
    svc = AuthUseCase(
        db=db_client,
        passwords=passwords,
        secret=settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    return await svc.authenticate(jwt_token=token)


async def use_db_session(req: Request):
    db_client = cast(AsgiApp, req.app).state.db
    async with db_client.begin_session() as session:
        yield session


def use_error_tracking(conn: Request):
    return cast(AsgiApp, conn.app).state.error_tracker


def use_interviews(req: Request):
    return cast(AsgiApp, req.app).state.interviews


def use_voice_sessions(req: Request):
    return cast(AsgiApp, req.app).state.voice_sessions


DBSessionDep = Annotated[AsyncSession, Depends(use_db_session)]
UserDep = Annotated[UserSchema, Depends(use_auth)]
ErrorTrackingDep = Annotated[ErrorTrackingPort, Depends(use_error_tracking)]
InterviewDep = Annotated[InterviewService, Depends(use_interviews)]
VoiceSessionsDep = Annotated[VoiceSessionService, Depends(use_voice_sessions)]
