from typing import Any, get_args

from app.ports import ErrorTrackingPort
from shared.types_ import ErrorTrackingBackendTypes

from .backends import BaseBackend, ConsoleBackend, SentryBackend

ALLOWED_BACKENDS: tuple[str, ...] = get_args(ErrorTrackingBackendTypes)


_BUILDER: dict[ErrorTrackingBackendTypes, type[BaseBackend]] = {
    "console": ConsoleBackend,
    "sentry": SentryBackend,
}


class ErrorTrackingAdapter(ErrorTrackingPort):
    def __init__(self, backend: ErrorTrackingBackendTypes, /, **kwargs: Any):
        if backend not in ALLOWED_BACKENDS:
            raise ValueError(
                f"Invalid value for backend. Possible values are: {ALLOWED_BACKENDS}"
            )

        self._backend = backend
        self._client = _BUILDER[backend](**kwargs)

    def init(self):
        self._client.init()

    def capture_exception(self, exc: BaseException, /, **context: Any):
        return self._client.capture_exception(exc, **context)
