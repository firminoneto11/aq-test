from typing import Any

import sentry_sdk

from .base import BaseBackend


class SentryBackend(BaseBackend):
    def __init__(self, **kwargs: Any):
        self._init_kwargs = kwargs
        self._initialized = False

    def init(self):
        sentry_sdk.init(**self._init_kwargs)
        self._initialized = True

    def capture_exception(self, exc: BaseException, /, **context: Any):
        """Best-effort capture for exceptions intentionally swallowed by the app."""
        try:
            import sentry_sdk

            client = sentry_sdk.get_client()
            if not client.is_active():
                return

            with sentry_sdk.new_scope() as scope:
                for key, value in context.items():
                    scope.set_extra(key, value)
                sentry_sdk.capture_exception(exc)
        except Exception:  # noqa
            return
