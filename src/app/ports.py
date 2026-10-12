from abc import ABCMeta, abstractmethod
from typing import (
    Any,
    AsyncContextManager,
    overload,
)

from httpx import Response
from langchain_core.tools import BaseTool
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from sqlalchemy.orm import DeclarativeBase


class HTTPClientPort(metaclass=ABCMeta):
    @abstractmethod
    async def get(
        self,
        endpoint: str = "",
        query_params: dict[str, Any] | None = None,
        expected_status_codes: tuple[int, ...] = (200,),
        headers: dict[str, Any] | None = None,
    ) -> tuple[Response, Exception | None]: ...

    @abstractmethod
    async def post(
        self,
        endpoint: str = "",
        json: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        query_params: dict[str, Any] | None = None,
        expected_status_codes: tuple[int, ...] = (200,),
        headers: dict[str, Any] | None = None,
    ) -> tuple[Response, Exception | None]: ...


class SqlDBClientPort(metaclass=ABCMeta):
    @property
    @abstractmethod
    def engine(self) -> AsyncEngine: ...

    @abstractmethod
    async def connect(self, **kwargs: Any) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    def begin_session(self) -> AsyncContextManager[AsyncSession]: ...

    @abstractmethod
    async def ping(self) -> None: ...

    @abstractmethod
    async def migrate(
        self, base_model: type[DeclarativeBase], drop: bool = False
    ) -> None: ...


class TemplateEnginePort(metaclass=ABCMeta):
    @abstractmethod
    def get_template_text(self, template_name: str) -> str: ...

    @abstractmethod
    def render(self, template_name: str, /, *_: Any, **kwargs: Any) -> str: ...

    @abstractmethod
    def render_string(self, template_text: str, /, *_: Any, **kwargs: Any) -> str: ...

    @abstractmethod
    def extract_tags_from_string(self, template_text: str) -> tuple[str, ...]: ...

    @abstractmethod
    def is_valid(self, template_text: str) -> bool: ...


class ErrorTrackingPort(metaclass=ABCMeta):
    @abstractmethod
    def init(self) -> None: ...

    @abstractmethod
    def capture_exception(self, exc: BaseException, /, **context: Any) -> None: ...


class LLMPort(metaclass=ABCMeta):
    @overload
    @abstractmethod
    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: None = None,
        schema_output: None = None,
    ) -> str: ...

    @overload
    @abstractmethod
    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: list[BaseTool] = ...,
        schema_output: dict[str, Any] = ...,
    ) -> dict[str, Any]: ...

    @overload
    @abstractmethod
    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: None = None,
        schema_output: dict[str, Any] = ...,
    ) -> dict[str, Any]: ...

    @abstractmethod
    async def generate_completion(
        self,
        system_message: str,
        user_message: str,
        tools: list[BaseTool] | None = None,
        schema_output: dict[str, Any] | None = None,
    ) -> dict[str, Any] | str: ...

    @abstractmethod
    def count_tokens(self, text: str) -> int: ...
