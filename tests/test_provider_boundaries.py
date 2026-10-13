"""Provider boundaries without provider credentials or outbound requests."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock
from typing import Any, cast
from uuid import uuid4

import httpx
import jwt
import pytest
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openrouter import ChatOpenRouter
from livekit import api
from openrouter.errors import ServiceUnavailableResponseError
from pydantic import SecretStr

from conf import settings
from infra.llm.adapter import LLMAdapter
from infra.voice import LiveKitGateway


@pytest.fixture
def openrouter(monkeypatch: pytest.MonkeyPatch) -> LLMAdapter:
    monkeypatch.setattr(settings, "OPENROUTER_API_KEY", SecretStr("local-fake-openrouter-key"))
    return LLMAdapter("openrouter", "openai/gpt-4.1-mini")


@pytest.fixture
def gateway(monkeypatch: pytest.MonkeyPatch) -> LiveKitGateway:
    monkeypatch.setattr(settings, "LIVEKIT_URL", "wss://test.livekit.invalid")
    monkeypatch.setattr(settings, "LIVEKIT_API_KEY", SecretStr("local-fake-key"))
    monkeypatch.setattr(
        settings, "LIVEKIT_API_SECRET", SecretStr("local-fake-signing-secret-with-32-bytes")
    )
    return LiveKitGateway()


SCHEMA = {
    "title": "Answer",
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


async def test_openrouter_structured_response_uses_strict_json_and_parses_once(
    openrouter: LLMAdapter, monkeypatch: pytest.MonkeyPatch
):
    attempts = []

    async def generate(
        self: ChatOpenRouter, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        attempts.append(kwargs["response_format"])
        assert isinstance(messages[0], SystemMessage)
        assert isinstance(messages[1], HumanMessage)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content='{"answer":"ok"}'))])

    monkeypatch.setattr(ChatOpenRouter, "_agenerate", generate)
    result = await openrouter.generate_completion("Instructions", "Candidate answer", schema_output=SCHEMA)
    assert result == {"answer": "ok"}
    assert len(attempts) == 1
    assert attempts[0]["type"] == "json_schema"
    assert attempts[0]["json_schema"]["strict"] is True
    assert attempts[0]["json_schema"]["schema"]["additionalProperties"] is False


async def test_openrouter_invalid_json_does_not_retry_and_preserves_parse_failure(
    openrouter: LLMAdapter, monkeypatch: pytest.MonkeyPatch
):
    attempts = 0

    async def generate(
        self: ChatOpenRouter, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        nonlocal attempts
        attempts += 1
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="not JSON"))])

    monkeypatch.setattr(ChatOpenRouter, "_agenerate", generate)
    # The application validates this None as invalid structured output and decides
    # whether to make its one corrective attempt. The transport must not retry it.
    assert await openrouter.generate_completion("Instructions", "Answer", schema_output=SCHEMA) is None
    assert attempts == 1


async def test_openrouter_slow_structured_request_is_cancelled_without_another_attempt(
    openrouter: LLMAdapter, monkeypatch: pytest.MonkeyPatch
):
    attempts = 0
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def generate(
        self: ChatOpenRouter, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        nonlocal attempts
        attempts += 1
        started.set()
        try:
            await asyncio.Event().wait()
            return ChatResult(generations=[])
        finally:
            cancelled.set()

    monkeypatch.setattr(ChatOpenRouter, "_agenerate", generate)
    task = asyncio.create_task(
        openrouter.generate_completion("Instructions", "Answer", schema_output=SCHEMA)
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cancelled.is_set()
    assert attempts == 1


async def test_openrouter_upstream_failure_is_not_retried_or_swallowed(
    openrouter: LLMAdapter, monkeypatch: pytest.MonkeyPatch
):
    attempts = 0
    failure = ConnectionError("provider unavailable")

    async def generate(
        self: ChatOpenRouter, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        nonlocal attempts
        attempts += 1
        raise failure

    monkeypatch.setattr(ChatOpenRouter, "_agenerate", generate)
    with pytest.raises(ConnectionError) as raised:
        await asyncio.wait_for(
            openrouter.generate_completion("Instructions", "Answer", schema_output=SCHEMA),
            timeout=5,
        )
    assert raised.value is failure
    assert attempts == 1


async def test_openrouter_sdk_attempts_unavailable_provider_once(
    openrouter: LLMAdapter,
):
    requests = 0

    def unavailable(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            503,
            json={"error": {"code": 503, "message": "provider unavailable"}},
        )

    sdk_config = cast(ChatOpenRouter, getattr(openrouter, "_model")).client.sdk_configuration
    original_client = sdk_config.async_client
    async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
        sdk_config.async_client = client
        try:
            with pytest.raises(ServiceUnavailableResponseError):
                await asyncio.wait_for(
                    openrouter.generate_completion(
                        "Instructions", "Candidate answer", schema_output=SCHEMA
                    ),
                    timeout=2,
                )
        finally:
            sdk_config.async_client = original_client
    assert requests == 1


async def test_text_completion_returns_provider_text_without_logging_private_content(
    openrouter: LLMAdapter, monkeypatch: pytest.MonkeyPatch
):
    from infra.llm import adapter as llm_module

    private_input = "private interview answer 92480"
    private_output = "private response 58279"
    events = SimpleNamespace(ainfo=AsyncMock(), exception=AsyncMock())
    monkeypatch.setattr(llm_module, "logger", events)

    async def generate(
        self: ChatOpenRouter, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(
                content=private_output,
                usage_metadata={"input_tokens": 12, "output_tokens": 3, "total_tokens": 15},
            ))]
        )

    monkeypatch.setattr(ChatOpenRouter, "_agenerate", generate)
    assert await openrouter.generate_completion("Instructions", private_input) == private_output
    events.ainfo.assert_awaited_once()
    event = events.ainfo.await_args
    assert event.args == ("llm.completion",)
    assert event.kwargs["input_tokens"] == 12
    assert event.kwargs["output_tokens"] == 3
    assert private_input not in str(event)
    assert private_output not in str(event)


async def test_text_completion_upstream_failure_propagates(
    openrouter: LLMAdapter, monkeypatch: pytest.MonkeyPatch
):
    failure = ValueError("provider rejected request")

    async def generate(
        self: ChatOpenRouter, messages: list[BaseMessage], stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None, **kwargs: Any,
    ) -> ChatResult:
        raise failure

    monkeypatch.setattr(ChatOpenRouter, "_agenerate", generate)
    with pytest.raises(ValueError) as raised:
        await openrouter.generate_completion("Instructions", "Candidate answer")
    assert raised.value is failure


@pytest.mark.parametrize(
    ("code", "should_raise"),
    [
        (api.ServerErrorCode.ALREADY_EXISTS, False),
        (api.ServerErrorCode.PERMISSION_DENIED, True),
    ],
)
async def test_create_room_only_tolerates_already_existing_room(
    gateway: LiveKitGateway, code: str, should_raise: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = api.ServerError(code, "room rejected", status=409 if not should_raise else 403)
    calls: list[api.CreateRoomRequest] = []

    async def create_room(request: api.CreateRoomRequest) -> None:
        calls.append(request)
        raise failure

    monkeypatch.setattr(
        gateway, "_get_client",
        lambda: SimpleNamespace(room=SimpleNamespace(create_room=create_room)),
    )
    if should_raise:
        with pytest.raises(api.ServerError) as raised:
            await gateway.create_room("interview-room")
        assert raised.value is failure
    else:
        await gateway.create_room("interview-room")
    assert len(calls) == 1
    assert calls[0].name == "interview-room"
    assert calls[0].max_participants == 2
    assert calls[0].empty_timeout == 60


async def test_dispatch_restricts_metadata_and_propagates_provider_failure(
    gateway: LiveKitGateway, monkeypatch: pytest.MonkeyPatch,
) -> None:
    interview_id, run_id = uuid4(), uuid4()
    calls: list[api.CreateAgentDispatchRequest] = []

    async def dispatch(request: api.CreateAgentDispatchRequest) -> SimpleNamespace:
        calls.append(request)
        return SimpleNamespace(id="worker-job-id")

    fake_dispatch = SimpleNamespace(create_dispatch=dispatch)
    monkeypatch.setattr(
        gateway, "_get_client",
        lambda: SimpleNamespace(agent_dispatch=fake_dispatch),
    )
    assert await gateway.dispatch("interview-room", interview_id, run_id) == "worker-job-id"
    assert len(calls) == 1
    request = calls[0]
    assert request.room == "interview-room"
    assert request.agent_name == "interviewer"
    assert json.loads(request.metadata) == {
        "interview_id": str(interview_id), "run_id": str(run_id)
    }

    failure = api.ServerError(api.ServerErrorCode.UNAVAILABLE, "dispatch unavailable", status=503)

    async def rejected(request: api.CreateAgentDispatchRequest) -> None:
        raise failure

    fake_dispatch.create_dispatch = rejected
    with pytest.raises(api.ServerError) as raised:
        await gateway.dispatch("interview-room", interview_id, run_id)
    assert raised.value is failure


@pytest.mark.parametrize(
    ("code", "should_raise"),
    [
        (api.ServerErrorCode.NOT_FOUND, False),
        (api.ServerErrorCode.UNAVAILABLE, True),
    ],
)
async def test_close_room_only_tolerates_absent_room(
    gateway: LiveKitGateway, code: str, should_raise: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure = api.ServerError(code, "room close failed", status=404 if not should_raise else 503)
    calls: list[str] = []

    async def delete_room(request: api.DeleteRoomRequest) -> None:
        calls.append(request.room)
        raise failure

    monkeypatch.setattr(
        gateway, "_get_client",
        lambda: SimpleNamespace(room=SimpleNamespace(delete_room=delete_room)),
    )
    if should_raise:
        with pytest.raises(api.ServerError) as raised:
            await gateway.close_room("interview-room")
        assert raised.value is failure
    else:
        await gateway.close_room("interview-room")
    assert calls == ["interview-room"]


async def test_gateway_closes_own_client_once_and_can_reopen(
    gateway: LiveKitGateway, monkeypatch: pytest.MonkeyPatch
):
    clients = []

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            self.closed = 0
            clients.append(self)

        async def aclose(self) -> None:
            self.closed += 1

    monkeypatch.setattr(api, "LiveKitAPI", Client)
    async with gateway:
        assert len(clients) == 1
    assert clients[0].closed == 1
    await gateway.aclose()
    assert clients[0].closed == 1
    async with gateway:
        assert len(clients) == 2
    await gateway.aclose()
    assert clients[1].closed == 1


@pytest.mark.parametrize("expiry", [-10, 0, 1])
def test_gateway_rejects_expired_grants(gateway: LiveKitGateway, expiry: int):
    with pytest.raises(ValueError, match="Grant expired"):
        gateway.issue_token(
            "interview-room", "candidate", datetime.now(UTC) + timedelta(seconds=expiry)
        )


def test_gateway_token_restricts_room_identity_and_absolute_expiry(gateway: LiveKitGateway):
    expiry = datetime.now(UTC) + timedelta(minutes=5)
    token = gateway.issue_token("interview-room", "candidate-identity", expiry)
    claims = jwt.decode(
        token, "local-fake-signing-secret-with-32-bytes", algorithms=["HS256"],
        options={"verify_aud": False},
    )
    assert claims["sub"] == "candidate-identity"
    assert claims["iss"] == "local-fake-key"
    assert 0 < claims["exp"] - datetime.now(UTC).timestamp() <= 300
    assert claims["exp"] <= expiry.timestamp()
    assert set(claims["video"]) == {
        "roomJoin", "room", "canPublish", "canSubscribe", "canPublishData", "canPublishSources"
    }
    assert claims["video"] == {
        "roomJoin": True,
        "room": "interview-room",
        "canPublish": True,
        "canSubscribe": True,
        "canPublishData": True,
        "canPublishSources": ["microphone"],
    }
    assert "sip" not in claims
    assert "inference" not in claims
    assert "observability" not in claims


def test_gateway_rejects_naive_expiry(gateway: LiveKitGateway):
    with pytest.raises(ValueError, match="timezone-aware"):
        gateway.issue_token("interview-room", "candidate", datetime.now() + timedelta(minutes=5))
