import time
from typing import TYPE_CHECKING, Any, cast, overload

from langchain_anthropic import ChatAnthropic
from langchain_core.messages import HumanMessage, SystemMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_openrouter import ChatOpenRouter

from app.ports import LLMPort
from conf import settings
from shared import logging

from . import enums

if TYPE_CHECKING:
    from langchain_core.language_models import LanguageModelInput
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, BaseMessage
    from langchain_core.runnables import Runnable
    from langchain_core.tools import BaseTool

    type StructuredModel = Runnable[LanguageModelInput, dict[str, Any]]


logger = logging.get_logger()


class LLMAdapter(LLMPort):
    def __init__(
        self, provider: enums.ALLOWED_PROVIDERS, model: str, **provider_kwargs: Any
    ):
        if not model:
            raise ValueError("You need to pass a value to the `model` property")
        if provider not in enums.ALLOWED_PROVIDERS_TPL:
            raise ValueError(
                "You need to specify a valid value for the `provider` property. "
                f"Valid options are {enums.ALLOWED_PROVIDERS_TPL!r}"
            )
        self._provider = provider
        self._model = self._get_model(provider=provider, model=model, **provider_kwargs)

    def _get_model(
        self, provider: enums.ALLOWED_PROVIDERS, model: str, **provider_kwargs: Any
    ):
        match provider:
            case "anthropic":
                return ChatAnthropic(
                    model_name=model,
                    api_key=settings.ANTHROPIC_API_KEY,
                    **provider_kwargs,
                )
            case "google":
                thinking_kwarg = {"thinking_budget": 0}
                if model.startswith("gemini-3"):
                    thinking_kwarg = {"thinking_level": "low"}
                return ChatGoogleGenerativeAI(
                    model=model,
                    api_key=settings.GOOGLE_API_KEY,
                    **{**thinking_kwarg, **provider_kwargs},
                )
            case "openrouter":
                return ChatOpenRouter(
                    model=model,
                    api_key=settings.OPENROUTER_API_KEY,
                    **provider_kwargs,
                )

    @overload
    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: None = None,
        schema_output: None = None,
        cache_system_prompt: bool = False,
    ) -> str: ...

    @overload
    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: list[BaseTool] = ...,
        schema_output: dict[str, Any] = ...,
        cache_system_prompt: bool = False,
    ) -> dict[str, Any]: ...

    @overload
    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: None = None,
        schema_output: dict[str, Any] = ...,
        cache_system_prompt: bool = False,
    ) -> dict[str, Any]: ...

    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: list[BaseTool] | None = None,
        schema_output: dict[str, Any] | None = None,
        cache_system_prompt: bool = False,
    ):
        if not system_message:
            raise ValueError("You must provide at least the system message")

        sys_msg: str | list[str | dict[str, Any]] = system_message
        if cache_system_prompt and self._provider == "anthropic":
            sys_msg = [
                {
                    "type": "text",
                    "text": system_message,
                    "cache_control": {"type": "ephemeral"},
                }
            ]

        messages: list[BaseMessage] = [SystemMessage(content=sys_msg)]

        if user_message:
            messages.append(HumanMessage(content=user_message))

        model = self._model

        if tools:
            if schema_output is None:
                raise ValueError("schema_output is required when tools are provided")
            model = model.bind_tools(tools)

        model = cast("BaseChatModel", model)

        t0 = time.monotonic()
        try:
            if schema_output:
                runnable = cast(
                    "StructuredModel",
                    model.with_structured_output(
                        schema=schema_output, method="json_schema", include_raw=True
                    ),
                ).with_retry()
                result = await runnable.ainvoke(input=messages)
                await self._log(result["raw"], time.monotonic() - t0)
                return cast(dict[str, Any], result["parsed"])

            runnable = model.with_retry()
            response = await runnable.ainvoke(messages)
            await self._log(response, time.monotonic() - t0)
            return str(response.text)
        except Exception as exc:
            logger.exception(
                "llm.completion.failed",
                error_msg=str(exc),
                latency_s=round(time.monotonic() - t0, 3),
            )
            raise

    def count_tokens(self, text: str):
        return self._model.get_num_tokens(text)

    async def _log(self, response: AIMessage, elapsed: float):
        usage = response.usage_metadata or {}
        meta = response.response_metadata or {}
        await logger.ainfo(
            "llm.completion",
            latency_s=round(elapsed, 3),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            cache_creation_tokens=meta.get("cache_creation_input_tokens"),
            cache_read_tokens=meta.get("cache_read_input_tokens"),
        )
