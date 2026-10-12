from secrets import token_hex
from typing import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from shared import logging

HEADER_NAME = "X-Request-Id"


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ):
        correlation_id = request.headers.get(HEADER_NAME) or token_hex(16)

        logging.bind_contextvars(correlation_id=correlation_id)

        try:
            response = await call_next(request)
            response.headers[HEADER_NAME] = correlation_id
            return response
        finally:
            logging.unbind_contextvars("correlation_id")
