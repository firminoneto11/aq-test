from datetime import UTC, datetime
from functools import partial, wraps
from math import floor
from typing import Awaitable, Callable, Literal, overload

import anyio
import tenacity
from anyio.to_thread import run_sync as run_in_thread


def asyncify[**Spec, R](function: Callable[Spec, R]):
    @wraps(function)
    async def _inner(*args: Spec.args, **kwargs: Spec.kwargs):
        func = partial(function, *args, **kwargs)
        return await run_in_thread(func)

    return _inner


def syncify[**Spec, R](function: Callable[Spec, Awaitable[R]]):
    @wraps(function)
    def _inner(*args: Spec.args, **kwargs: Spec.kwargs):
        func = partial(function, *args, **kwargs)
        return anyio.run(func=func)

    return _inner


@overload
def datetime_utc(unix: Literal[True]) -> int: ...


@overload
def datetime_utc(unix: Literal[False]) -> datetime: ...


def datetime_utc(unix: bool = False):
    utc_now = datetime.now(tz=UTC)
    if unix:
        utc_now = floor(utc_now.timestamp())
    return utc_now


def configure_retries[**Spec, R](
    function: Callable[Spec, R],
    max_attempts: int,
    initial_delay: float,
    exponent: int,
    retry_on_exceptions: type[BaseException]
    | tuple[type[BaseException], ...]
    | None = None,
):
    if retry_on_exceptions is None:
        retry_on_exceptions = (Exception,)

    config = tenacity.Retrying(
        stop=tenacity.stop_after_attempt(max_attempt_number=max_attempts),
        wait=tenacity.wait_exponential_jitter(initial=initial_delay, exp_base=exponent),
        retry=tenacity.retry_if_exception_type(exception_types=retry_on_exceptions),
        reraise=True,
    )

    return config.wraps(function)
