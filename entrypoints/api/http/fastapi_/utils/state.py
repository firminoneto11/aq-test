from typing import Annotated, cast

from fastapi import Depends
from fastapi.requests import Request
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.ports import ErrorTrackingPort, SqlDBClientPort
from app.usecases.auth import AuthUseCase
from conf import settings
from domain.schemas import UserSchema
from entrypoints.api.http.fastapi_.types_ import AsgiApp

_oauth2_scheme = OAuth2PasswordBearer(tokenUrl="https://external-auth-service/token")


def use_auth(token: Annotated[str, Depends(_oauth2_scheme)]):
    svc = AuthUseCase(secret=settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return svc.authenticate(jwt_token=token)


def use_db_client(req: Request):
    return cast(AsgiApp, req.app).state.db


async def use_db_session(req: Request):
    db_client = cast(AsgiApp, req.app).state.db
    async with db_client.begin_session() as session:
        yield session


def use_error_tracking(conn: Request):
    return cast(AsgiApp, conn.app).state.error_tracker


DBClientDep = Annotated[SqlDBClientPort, Depends(use_db_client)]
DBSessionDep = Annotated[AsyncSession, Depends(use_db_session)]
UserDep = Annotated[UserSchema, Depends(use_auth)]
ErrorTrackingDep = Annotated[ErrorTrackingPort, Depends(use_error_tracking)]
