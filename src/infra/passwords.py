import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from app.ports import PasswordPort


class PasswordAdapter(PasswordPort):
    def __init__(self):
        self._hasher = PasswordHasher()
        self._dummy_hash = self._hasher.hash("unknown-account")

    async def hash_password(self, password: str) -> str:
        return await asyncio.to_thread(self._hasher.hash, password)

    async def verify_password(self, password_hash: str | None, password: str) -> bool:
        try:
            matched = await asyncio.to_thread(
                self._hasher.verify, password_hash or self._dummy_hash, password
            )
        except VerificationError, InvalidHashError:
            return False
        return bool(matched and password_hash is not None)

    def needs_rehash(self, password_hash: str) -> bool:
        return self._hasher.check_needs_rehash(password_hash)
