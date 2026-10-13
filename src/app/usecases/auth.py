from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID

import jwt
import sqlalchemy as sa
from email_validator import EmailNotValidError, validate_email
from sqlalchemy.exc import IntegrityError

from app.exceptions import GenericException
from app.ports import PasswordPort, SqlDBClientPort
from domain.models import User
from domain.schemas import TokenSchema, UserSchema

_ISSUER = "aq-interviewer"
_AUDIENCE = "aq-interviewer-api"
_ACCESS_TOKEN_LIFETIME = 86400
_REQUIRED_CLAIMS = ["sub", "email", "iat", "exp", "iss", "aud"]


def validate_jwt_settings(secret: str, algorithm: str) -> None:
    if algorithm != "HS256" or len(secret.encode()) < 32:
        raise ValueError("JWT_SECRET must contain at least 32 bytes and use HS256")


def normalize_email(email: str) -> str:
    try:
        validated = validate_email(email.strip().lower(), check_deliverability=False)
    except EmailNotValidError as exc:
        raise ValueError("Invalid email address") from exc
    if not validated.normalized or len(validated.normalized) > 320:
        raise ValueError("Invalid email address")
    return validated.normalized


@dataclass
class AuthUseCase:
    db: SqlDBClientPort
    passwords: PasswordPort
    secret: str
    algorithm: str

    def __post_init__(self):
        validate_jwt_settings(self.secret, self.algorithm)

    async def authenticate(self, jwt_token: str) -> UserSchema:
        try:
            payload = jwt.decode(
                jwt_token,
                self.secret,
                algorithms=["HS256"],
                issuer=_ISSUER,
                audience=_AUDIENCE,
                options={"require": _REQUIRED_CLAIMS},
            )
        except jwt.ExpiredSignatureError as exc:
            raise GenericException(detail="Token has expired", code=401) from exc
        except jwt.MissingRequiredClaimError as exc:
            raise GenericException(detail="Token missing claim", code=401) from exc
        except jwt.InvalidTokenError as exc:
            raise GenericException(detail="Invalid token", code=401) from exc

        try:
            user_id = UUID(payload["sub"])
            email = payload["email"]
        except (TypeError, ValueError, KeyError) as exc:
            raise GenericException(detail="Invalid token", code=401) from exc

        async with self.db.begin_session() as session:
            user = await session.get(User, user_id)
            if user is None or not user.is_active or user.email != email:
                raise GenericException(detail="Invalid token", code=401)
            return UserSchema(id=user.id, email=user.email)

    async def login(self, email: str, password: str) -> TokenSchema:
        try:
            normalized = normalize_email(email)
        except ValueError as exc:
            raise GenericException(detail="Invalid credentials", code=401) from exc

        async with self.db.begin_session() as session:
            user = await session.scalar(sa.select(User).where(User.email == normalized))
            if not await self.passwords.verify_password(
                user.password_hash if user is not None else None, password
            ):
                raise GenericException(detail="Invalid credentials", code=401)
            if user is None or not user.is_active:
                raise GenericException(detail="Invalid credentials", code=401)
            if self.passwords.needs_rehash(user.password_hash):
                user.password_hash = await self.passwords.hash_password(password)

            now = datetime.now(UTC)
            token = jwt.encode(
                {
                    "sub": str(user.id),
                    "email": user.email,
                    "iat": now,
                    "exp": now + timedelta(seconds=_ACCESS_TOKEN_LIFETIME),
                    "iss": _ISSUER,
                    "aud": _AUDIENCE,
                },
                self.secret,
                algorithm="HS256",
            )
            return TokenSchema(access_token=token, expires_in=_ACCESS_TOKEN_LIFETIME)

    async def create_user(self, email: str, password: str) -> UserSchema:
        normalized = normalize_email(email)
        if not 12 <= len(password) <= 128:
            raise ValueError("Password must be 12 to 128 characters long")

        password_hash = await self.passwords.hash_password(password)
        try:
            async with self.db.begin_session() as session:
                user = User(email=normalized, password_hash=password_hash)
                session.add(user)
                await session.flush()
                result = UserSchema(id=user.id, email=user.email)
        except IntegrityError as exc:
            raise GenericException(detail="Email already exists", code=409) from exc
        return result
