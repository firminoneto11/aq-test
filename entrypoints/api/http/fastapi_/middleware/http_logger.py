from typing import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from shared import logging

logger = logging.get_logger()


class HttpLoggerMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ):
        client_ip = self._get_ip_address(request=request)

        logging.bind_contextvars(client_ip=client_ip)

        try:
            response = await call_next(request)
            await logger.ainfo(
                "http.request",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
            )
            return response
        finally:
            logging.unbind_contextvars("client_ip")

    def _get_ip_address(self, request: Request):
        forwarded_for = request.headers.get("X-Forwarded-For", "")

        if forwarded_for:
            forwarded_for = forwarded_for.split(",")[0].strip()

        if forwarded_for:
            return forwarded_for

        return request.client.host if request.client else "-"
