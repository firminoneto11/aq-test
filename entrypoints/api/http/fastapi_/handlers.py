from typing import cast

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import ExceptionHandler

from app.exceptions import GenericException


async def handle_generic_exception(request: Request, exc: GenericException):
    return JSONResponse(status_code=exc.code, content={"detail": exc.detail})


HANDLERS_MAP = {
    GenericException: cast(ExceptionHandler, handle_generic_exception),
}
