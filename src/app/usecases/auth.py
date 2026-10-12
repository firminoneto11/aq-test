from dataclasses import dataclass

import jwt
from pydantic import ValidationError

from app.exceptions import GenericException
from domain.schemas import UserSchema


@dataclass
class AuthUseCase:
    secret: str
    algorithm: str

    def authenticate(self, jwt_token: str):
        try:
            payload = jwt.decode(jwt_token, self.secret, algorithms=[self.algorithm])
        except jwt.ExpiredSignatureError:
            raise GenericException(detail="Token has expired", code=401)
        except jwt.InvalidTokenError:
            raise GenericException(detail="Invalid token", code=401)

        try:
            return UserSchema(**payload)
        except ValidationError as exc:
            raise GenericException(detail="Token missing claim", code=401) from exc
