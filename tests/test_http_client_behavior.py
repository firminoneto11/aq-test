"""Consumer-facing HTTP adapter behavior without external network access."""

import json
from functools import partial
from typing import Any, Callable

import httpx
import pytest

import infra.http_client.adapter as http_adapter
from infra.http_client.adapter import HttpClientAdapter
from infra.http_client.exceptions import HTTPError, InvalidHTTPStatusError


def client_for(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    **options: Any,
) -> HttpClientAdapter:
    # The adapter owns and closes each client; only its network transport is replaced.
    monkeypatch.setattr(
        http_adapter,
        "AsyncClient",
        partial(httpx.AsyncClient, transport=httpx.MockTransport(handler)),
    )
    settings: dict[str, Any] = {
        "base_url": "https://service.example/api/",
        "headers": {"Authorization": "Bearer project", "X-Trace": "base"},
        "max_attempts": 3,
        "initial_delay": 0.001,
    }
    settings.update(options)
    return HttpClientAdapter(**settings)


async def test_get_and_post_send_configured_request_and_accept_alternate_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET":
            return httpx.Response(206, json={"items": [1]})
        return httpx.Response(201, json={"id": 7})

    client = client_for(monkeypatch, handle)
    fetched, fetch_error = await client.get(
        "items", query_params={"page": 2}, headers={"X-Trace": "get"},
        expected_status_codes=(200, 206),
    )
    created, create_error = await client.post(
        "items", json={"name": "new"}, query_params={"draft": "yes"},
        headers={"X-Trace": "post"}, expected_status_codes=(201,),
    )

    assert fetch_error is None
    assert fetched.status_code == 206
    assert fetched.json() == {"items": [1]}
    assert create_error is None
    assert created.status_code == 201
    assert created.json() == {"id": 7}
    assert len(requests) == 2
    get_request, post_request = requests
    assert get_request.method == "GET"
    assert str(get_request.url) == "https://service.example/api/items?page=2"
    assert get_request.headers["authorization"] == "Bearer project"
    assert get_request.headers["x-trace"] == "get"
    assert post_request.method == "POST"
    assert str(post_request.url) == "https://service.example/api/items?draft=yes"
    assert post_request.headers["authorization"] == "Bearer project"
    assert post_request.headers["x-trace"] == "post"
    assert json.loads(post_request.content) == {"name": "new"}


async def test_post_form_data_is_sent_as_form_not_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json={"accepted": True})

    client = client_for(monkeypatch, handle)
    response, error = await client.post("submit", data={"answer": "two words"})

    assert error is None
    assert response.json() == {"accepted": True}
    assert len(observed) == 1
    assert observed[0].headers["content-type"].startswith("application/x-www-form-urlencoded")
    assert observed[0].content == b"answer=two+words"


@pytest.mark.parametrize(
    ("body", "headers", "expected_text"),
    [
        ({"detail": "missing"}, None, '{"detail": "missing"}'),
        ("not available", None, "not available"),
        ("{broken", {"content-type": "application/json"}, "{broken"),
    ],
)
async def test_unexpected_client_error_preserves_status_and_payload_in_error(
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, str] | str,
    headers: dict[str, str] | None,
    expected_text: str,
) -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if isinstance(body, dict):
            return httpx.Response(404, json=body, headers=headers)
        return httpx.Response(404, text=body, headers=headers)

    client = client_for(monkeypatch, handle)
    response, error = await client.get("missing")

    assert isinstance(error, InvalidHTTPStatusError)
    assert error.status_code == 404
    assert error.response_text == expected_text
    assert response.is_error


async def test_successful_non_json_body_remains_available_without_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = client_for(
        monkeypatch,
        lambda request: httpx.Response(
            200, text="{truncated", headers={"content-type": "application/json"}
        ),
    )

    response, error = await client.get("report")

    assert error is None
    assert response.status_code == 200
    assert response.text == "{truncated"
    with pytest.raises(ValueError):
        response.json()


async def test_transient_transport_failure_retries_and_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise httpx.ConnectError("temporarily offline", request=request)
        return httpx.Response(200, json={"restored": True})

    client = client_for(monkeypatch, handle)
    response, error = await client.get("health")

    assert attempts == 3
    assert error is None
    assert response.json() == {"restored": True}


async def test_transport_failure_stops_at_attempt_limit_and_returns_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("temporarily offline", request=request)

    client = client_for(monkeypatch, handle, max_attempts=2)
    response, error = await client.post("submit", json={"task": 1})

    assert attempts == 2
    assert response.is_error
    assert isinstance(error, HTTPError)
    assert error.response_text == "temporarily offline"
