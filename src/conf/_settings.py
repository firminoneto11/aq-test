from importlib.metadata import metadata
from pathlib import Path
from typing import Any, cast

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.types_ import ErrorTrackingBackendTypes

from ._types import ALLOWED_ENVIRONMENTS, HTTPBackendFrameworkTypes

project = metadata("aq-test")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = project["Name"]
    DESCRIPTION: str = project["Summary"]
    VERSION: str = project["Version"]
    COMMIT_ID: str = "unknown"
    BRANCH_NAME: str = "unknown"

    APP_DIR: Path = Path(__file__).parent.parent
    APP_TIMEZONE: str = "UTC"

    CONNECT_TO_SERVICES_ON_STARTUP: bool = True

    HTTP_BACKEND_FRAMEWORK: HTTPBackendFrameworkTypes = "fastapi"

    API_PREFIX: str = "/api"
    DOCS_URL: str = "/_internal/docs"
    OPENAPI_URL: str = "/_internal/openapi.json"

    ENVIRONMENT: ALLOWED_ENVIRONMENTS = "development"

    AUTOCOMMIT: bool = True

    JWT_SECRET: str = ""
    JWT_ALGORITHM: str = "HS256"

    ALLOWED_ORIGINS: list[str] = ["http://localhost:5173"]

    ASGI_APP: str = "entrypoints.api.http.asgi:app"
    HOST: str = "127.0.0.1"
    PORT: int = 8000
    HTTP_WORKERS: int | None = None

    DEBUG: bool = False

    GOOGLE_API_KEY: SecretStr = SecretStr("")
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")
    OPENROUTER_API_KEY: SecretStr = SecretStr("")
    LIVEKIT_URL: str = ""
    LIVEKIT_API_KEY: SecretStr = SecretStr("")
    LIVEKIT_API_SECRET: SecretStr = SecretStr("")
    DEEPGRAM_API_KEY: SecretStr = SecretStr("")
    ELEVEN_API_KEY: SecretStr = SecretStr("")

    INTERVIEW_LLM_MODEL: str = "openai/gpt-6-luna"
    INTERVIEW_PLANNER_MODEL: str = "openai/gpt-6-luna"
    DEEPGRAM_MODEL: str = "nova-3"
    ELEVEN_MODEL: str = "eleven_turbo_v2_5"
    ELEVEN_VOICE_ID: str = "hpp4J3VqNfWAUOO0d1Us"
    VOICE_MAX_ACTIVE: int = 2
    VOICE_LEASE_SECONDS: int = 30
    VOICE_HEARTBEAT_SECONDS: int = 10
    LLM_TIMEOUT_SECONDS: int = 20
    EVALUATION_TIMEOUT_SECONDS: int = 75

    def get_asgi_settings(self, main_mount: bool):
        docs_url = None if main_mount else self.DOCS_URL
        redoc_url = None
        openapi_url = None if main_mount else self.OPENAPI_URL

        if self.ENVIRONMENT in ("production", "staging"):
            docs_url, openapi_url = None, None

        return {
            "title": self.APP_NAME,
            "description": self.DESCRIPTION,
            "version": self.VERSION,
            "docs_url": docs_url,
            "redoc_url": redoc_url,
            "openapi_url": openapi_url,
            "debug": self.DEBUG,
        }

    @property
    def should_reload(self):
        return self.ENVIRONMENT == "development"

    @property
    def is_local(self):
        return self.ENVIRONMENT in ("development", "testing")

    # SQL Database section
    DB_DRIVER: str = "changeMe"
    DB_SERVER: str = "changeMe"

    @property
    def DATABASE_URL(self):
        scheme, separator, address = self.DB_SERVER.partition("://")
        if separator and scheme in ("postgres", "postgresql"):
            return f"postgresql+{self.DB_DRIVER}://{address}"
        return self.DB_SERVER.replace("+driver", f"+{self.DB_DRIVER}")

    # --

    # Error tracking section
    ERROR_TRACKER_BACKEND: ErrorTrackingBackendTypes = "console"
    SENTRY_DSN: str = "changeMe"
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    SENTRY_PROFILES_SAMPLE_RATING: float = 0.0
    SENTRY_SEND_DEFAULT_PII: bool = True
    SENTRY_ATTACH_STACK_TRACE: bool = True
    SENTRY_MAX_BREADCRUMBS: int = 50

    @property
    def error_tracker_kwargs(self):
        if self.ERROR_TRACKER_BACKEND == "sentry":
            return cast(
                dict[str, Any],
                {
                    "dsn": self.SENTRY_DSN,
                    "environment": self.ENVIRONMENT,
                    "release": self.COMMIT_ID,
                    "traces_sample_rate": self.SENTRY_TRACES_SAMPLE_RATE,
                    "profiles_sample_rate": self.SENTRY_PROFILES_SAMPLE_RATING,
                    "attach_stacktrace": self.SENTRY_ATTACH_STACK_TRACE,
                    "max_breadcrumbs": self.SENTRY_MAX_BREADCRUMBS,
                    "send_default_pii": self.SENTRY_SEND_DEFAULT_PII,
                },
            )
        return cast(dict[str, Any], {})

    # --

    # Logging section
    LOG_LEVEL: str = "INFO"

    def get_service_name(self, suffix: str = ""):
        return f"{self.APP_NAME}{'@' + suffix if suffix else suffix}"

    # --


settings = Settings()
