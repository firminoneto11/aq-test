import json as jsonlib
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Literal, cast

from httpx import AsyncClient, Response

from app.ports import HTTPClientPort
from shared.utils import configure_retries

from .exceptions import BaseExc, HTTPError, InvalidHTTPStatusError

EXPECTED_CODES = (200,)


@dataclass
class HttpClientAdapter(HTTPClientPort):
    base_url: str = ""
    headers: dict[str, Any] | None = None
    timeout: float = 30
    max_attempts: int = 3
    initial_delay: float = 1
    exponent: int = 3

    async def get(
        self,
        endpoint: str = "",
        query_params: dict[str, Any] | None = None,
        expected_status_codes: tuple[int, ...] = EXPECTED_CODES,
        headers: dict[str, Any] | None = None,
    ):
        return await self._make_request(
            method="GET",
            endpoint=endpoint,
            query_params=query_params,
            expected_status_codes=expected_status_codes,
            headers=headers,
        )

    async def post(
        self,
        endpoint: str = "",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        expected_status_codes: tuple[int, ...] = EXPECTED_CODES,
        headers: dict[str, Any] | None = None,
    ):
        return await self._make_request(
            method="POST",
            endpoint=endpoint,
            json=json,
            data=data,
            query_params=query_params,
            expected_status_codes=expected_status_codes,
            headers=headers,
        )

    async def _make_request(
        self,
        method: Literal["GET", "POST"],
        endpoint: str,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        expected_status_codes: tuple[int, ...] = EXPECTED_CODES,
        headers: dict[str, Any] | None = None,
    ):
        if method.strip().upper() not in ("GET", "POST"):
            raise ValueError(f"The {method!r} method is not allowed")

        if json and data:
            raise ValueError("Can not set both 'json' and 'data'")

        base_headers = self.headers or {}
        if headers:
            base_headers = {**base_headers, **headers}

        err = None
        fetch = configure_retries(
            function=self._fetch,
            max_attempts=self.max_attempts,
            initial_delay=self.initial_delay,
            exponent=self.exponent,
        )

        try:
            response = await fetch(
                client_kwargs={
                    "base_url": self.base_url,
                    "headers": base_headers,
                    "timeout": self.timeout,
                },
                method=method,
                endpoint=endpoint,
                json=json,
                data=data,
                query_params=query_params,
                expected_status_codes=expected_status_codes,
            )
        except BaseExc as exc:
            response, err = Response(500, text=exc.error_message), exc

        return response, err

    async def _fetch(
        self,
        client_kwargs: dict[str, Any],
        method: Literal["GET", "POST"],
        endpoint: str,
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        expected_status_codes: tuple[int, ...] = (200,),
    ):
        kwargs = {}

        if json:
            kwargs["json"] = json
        if data:
            kwargs["data"] = data
        if query_params:
            kwargs["params"] = query_params

        async with AsyncClient(**client_kwargs) as client:
            call = cast(
                Callable[..., Awaitable[Response]],
                getattr(client, method.strip().lower()),
            )
            try:
                response = await call(endpoint, **kwargs)
            except Exception as exc:
                raise HTTPError(
                    detail=(f"The {endpoint!r} API raised an unexpected error: {exc}"),
                    response_text=str(exc),
                ) from exc

        if response.status_code in expected_status_codes:
            return response

        try:
            text = jsonlib.dumps(response.json())
        except:  # noqa
            text = response.text

        raise InvalidHTTPStatusError(
            detail=(
                f"The {endpoint!r} API returned an unexpected status code response: "
                f"{response.status_code!r}"
            ),
            status_code=response.status_code,
            response_text=text,
        )
