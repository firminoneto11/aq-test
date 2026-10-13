from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.security import OAuth2PasswordRequestForm

from app.usecases.auth import AuthUseCase
from conf import settings
from domain.schemas import TokenSchema, UserSchema
from entrypoints.api.http.fastapi_.utils.state import DBClientDep, PasswordDep, UserDep

router = APIRouter(prefix="/auth")


@router.post("/token", response_model=TokenSchema)
async def login(
    form: Annotated[OAuth2PasswordRequestForm, Depends()],
    db: DBClientDep,
    passwords: PasswordDep,
) -> TokenSchema:
    service = AuthUseCase(
        db=db,
        passwords=passwords,
        secret=settings.JWT_SECRET,
        algorithm=settings.JWT_ALGORITHM,
    )
    return await service.login(email=form.username, password=form.password)


@router.get("/me", response_model=UserSchema)
async def current_user(user: UserDep) -> UserSchema:
    return user
