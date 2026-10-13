from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from argon2 import PasswordHasher
import jwt
import sqlalchemy as sa
from httpx import AsyncClient
import pytest

from app.exceptions import GenericException
from app.ports import PasswordPort, SqlDBClientPort
from app.usecases.auth import AuthUseCase
from conf import settings
from domain.models import User
from infra.passwords import PasswordAdapter
if TYPE_CHECKING:
    from entrypoints.api.http.fastapi_.factory import ASGIFactory
    from entrypoints.api.http.fastapi_.types_ import AsgiApp




async def _provision(
    db: SqlDBClientPort, passwords: PasswordPort, email: str, password: str
) -> UUID:
    service = AuthUseCase(
        db=db, passwords=passwords, secret=settings.JWT_SECRET, algorithm="HS256"
    )
    return (await service.create_user(email, password)).id


async def _remove(db: SqlDBClientPort, user_id: UUID) -> None:
    async with db.begin_session() as session:
        await session.execute(sa.delete(User).where(User.id == user_id))


async def test_login_issues_working_token_and_hides_unknown_account(
    http_client: AsyncClient,
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
):
    _, app = asgi_config
    email = f"reviewer-{uuid4()}@example.com"
    password = "correct-horse-battery-staple"
    user_id = await _provision(db, app.state.passwords, email, password)
    try:
        response = await http_client.post(
            "/api/v1/auth/token", data={"username": email.upper(), "password": password}
        )
        assert response.status_code == 200
        token = response.json()
        assert token["token_type"] == "bearer"
        assert token["expires_in"] == 86400
        me = await http_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {token['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json() == {"id": str(user_id), "email": email}

        for candidate in (email, f"unknown-{uuid4()}@example.com"):
            denied = await http_client.post(
                "/api/v1/auth/token",
                data={"username": candidate, "password": "wrong-password"},
            )
            assert denied.status_code == 401
            assert denied.json() == {"detail": "Invalid credentials"}
    finally:
        await _remove(db, user_id)


async def test_expired_invalid_and_disabled_credentials_cannot_read_user(
    http_client: AsyncClient,
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
):
    _, app = asgi_config
    email = f"reviewer-{uuid4()}@example.com"
    user_id = await _provision(
        db, app.state.passwords, email, "correct-horse-battery-staple"
    )
    now = datetime.now(UTC)
    claims = {
        "sub": str(user_id),
        "email": email,
        "iat": now,
        "exp": now + timedelta(hours=1),
        "iss": "aq-interviewer",
        "aud": "aq-interviewer-api",
    }

    try:
        cases = (
            ({**claims, "exp": now - timedelta(minutes=1)}, "Token has expired"),
            ({key: val for key, val in claims.items() if key != "sub"}, "Token missing claim"),
            ({**claims, "aud": "another-app"}, "Invalid token"),
            ({**claims, "sub": str(uuid4())}, "Invalid token"),
        )
        for payload, detail in cases:
            token = jwt.encode(payload, settings.JWT_SECRET, algorithm="HS256")
            response = await http_client.get(
                "/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 401
            assert response.json() == {"detail": detail}

        async with db.begin_session() as session:
            user = await session.get(User, user_id)
            assert user is not None
            user.is_active = False
        valid_token = jwt.encode(claims, settings.JWT_SECRET, algorithm="HS256")
        disabled = await http_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {valid_token}"},
        )
        assert disabled.status_code == 401
        login = await http_client.post(
            "/api/v1/auth/token",
            data={"username": email, "password": "correct-horse-battery-staple"},
        )
        assert login.status_code == 401
        assert login.json() == {"detail": "Invalid credentials"}
    finally:
        await _remove(db, user_id)


async def test_signup_rejects_duplicate_email_and_invalid_password_without_replacing_account(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
) -> None:
    _, app = asgi_config
    service = AuthUseCase(
        db, app.state.passwords, settings.JWT_SECRET, "HS256",
    )
    email = f"account-{uuid4()}@example.com"
    original_password = "correct-horse-battery-staple"
    first = await service.create_user(email.upper(), original_password)
    try:
        with pytest.raises(ValueError, match="Password"):
            await service.create_user("another@example.com", "too-short")
        with pytest.raises(GenericException) as duplicated:
            await service.create_user(email, "a-different-valid-password")
        assert duplicated.value.code == 409
        assert (await service.login(email, original_password)).access_token
        with pytest.raises(GenericException) as rejected:
            await service.login(email, "a-different-valid-password")
        assert rejected.value.code == 401
        async with db.begin_session() as session:
            users = (await session.scalars(sa.select(User).where(User.email == email))).all()
            assert len(users) == 1 and users[0].id == first.id
    finally:
        await _remove(db, first.id)


async def test_successful_login_upgrades_legacy_argon2_hash(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
) -> None:
    _, app = asgi_config
    password = "correct-horse-battery-staple"
    old_hash = PasswordHasher(time_cost=1, memory_cost=1024, parallelism=1).hash(password)
    email = f"old-hash-{uuid4()}@example.com"
    async with db.begin_session() as session:
        user = User(email=email, password_hash=old_hash)
        session.add(user)
        await session.flush()
        user_id = user.id
    try:
        service = AuthUseCase(db, app.state.passwords, settings.JWT_SECRET, "HS256")
        token = await service.login(email, password)
        assert (await service.authenticate(token.access_token)).id == user_id
        async with db.begin_session() as session:
            upgraded = await session.get(User, user_id)
            assert upgraded is not None and upgraded.password_hash != old_hash
            assert not PasswordAdapter().needs_rehash(upgraded.password_hash)
    finally:
        await _remove(db, user_id)


async def test_signed_token_with_invalid_subject_or_changed_email_is_denied(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
) -> None:
    _, app = asgi_config
    email = f"claims-{uuid4()}@example.com"
    service = AuthUseCase(db, app.state.passwords, settings.JWT_SECRET, "HS256")
    created = await service.create_user(email, "correct-horse-battery-staple")
    try:
        now = datetime.now(UTC)
        claims = {
            "sub": str(created.id), "email": email, "iat": now,
            "exp": now + timedelta(hours=1), "iss": "aq-interviewer",
            "aud": "aq-interviewer-api",
        }
        for wrong in ({**claims, "sub": "not-a-uuid"}, {**claims, "email": "other@example.com"}):
            token = jwt.encode(wrong, settings.JWT_SECRET, algorithm="HS256")
            with pytest.raises(GenericException) as rejected:
                await service.authenticate(token)
            assert rejected.value.code == 401
    finally:
        await _remove(db, created.id)


async def test_unsafe_jwt_configuration_and_malformed_email_are_rejected(
    db: SqlDBClientPort,
    asgi_config: tuple[ASGIFactory, AsgiApp],
) -> None:
    _, app = asgi_config
    with pytest.raises(ValueError):
        AuthUseCase(db, app.state.passwords, "short-key", "HS256")
    with pytest.raises(ValueError):
        AuthUseCase(db, app.state.passwords, settings.JWT_SECRET, "none")

    service = AuthUseCase(db, app.state.passwords, settings.JWT_SECRET, "HS256")
    with pytest.raises(ValueError, match="Invalid email"):
        await service.create_user("not-an-email", "correct-horse-battery-staple")
    with pytest.raises(GenericException) as invalid_login:
        await service.login("not-an-email", "correct-horse-battery-staple")
    assert invalid_login.value.code == 401
