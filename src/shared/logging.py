import sys
import tomllib
from typing import Any

import structlog

with open("pyproject.toml", mode="r") as file_stream:
    _SERVICE_NAME: str = tomllib.loads(file_stream.read())["project"]["name"]


_set = False


def configure_logging(
    log_level: str, use_simple_logging: bool = True, service_name: str | None = None
):
    global _SERVICE_NAME
    global _set

    if _set:
        return

    _SERVICE_NAME = service_name or _SERVICE_NAME  # pyright: ignore[reportConstantRedefinition]
    _set = True

    if use_simple_logging:
        return _configure_simple_logging(log_level=log_level)

    return _configure_default_logging(log_level=log_level)


def _configure_simple_logging(log_level: str):
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_service_name_processor,
            structlog.processors.add_log_level,
            structlog.processors.format_exc_info,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.UnicodeDecoder(),
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.dev.ConsoleRenderer(),
        ],
    )


def _configure_default_logging(log_level: str):
    structlog.configure(
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
        processors=[
            structlog.contextvars.merge_contextvars,
            _add_service_name_processor,
            structlog.processors.add_log_level,
            structlog.processors.format_exc_info,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.UnicodeDecoder(),
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.EventRenamer("message"),
            structlog.processors.JSONRenderer(),
        ],
    )


def get_logger(**kwargs: Any) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(**kwargs)


def bind_contextvars(**kwargs: Any):
    structlog.contextvars.bind_contextvars(**kwargs)


def unbind_contextvars(*args: Any):
    structlog.contextvars.unbind_contextvars(*args)


def _add_service_name_processor(
    logger: structlog.stdlib.BoundLogger, _: str, event_dict: structlog.types.EventDict
):
    if _SERVICE_NAME:
        event_dict.setdefault("service", _SERVICE_NAME)
    return event_dict
