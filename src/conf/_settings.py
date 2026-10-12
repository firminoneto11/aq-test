import tomllib
from pathlib import Path
from typing import Any, cast

from git import Repo
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from shared.types_ import ErrorTrackingBackendTypes

from ._types import ALLOWED_ENVIRONMENTS, HTTPBackendFrameworkTypes

repo = Repo(".")

with open("pyproject.toml", mode="r") as file_stream:
    project = tomllib.loads(file_stream.read())["project"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    APP_NAME: str = project["name"]
    DESCRIPTION: str = project["description"]
    VERSION: str = project["version"]
    COMMIT_ID: str = repo.head.commit.hexsha
    BRANCH_NAME: str = repo.active_branch.name

    APP_DIR: Path = Path(__file__).parent.parent
    APP_TIMEZONE: str = "UTC"

    CONNECT_TO_SERVICES_ON_STARTUP: bool = True

    HTTP_BACKEND_FRAMEWORK: HTTPBackendFrameworkTypes = "fastapi"

    API_PREFIX: str = "/api"
    DOCS_URL: str = "/_internal/docs"
    OPENAPI_URL: str = "/_internal/openapi.json"

    ENVIRONMENT: ALLOWED_ENVIRONMENTS = "development"

    AUTOCOMMIT: bool = True

    JWT_SECRET: str = "changeMe"
    JWT_ALGORITHM: str = "HS256"

    ALLOWED_ORIGINS: list[str] = ["*"]

    ASGI_APP: str = "entrypoints.api.http.asgi:app"
    HOST: str = "127.0.0.1"
    PORT: int = 8000

    DEBUG: bool = False

    GOOGLE_API_KEY: SecretStr = SecretStr("")
    ANTHROPIC_API_KEY: SecretStr = SecretStr("")
    OPENROUTER_API_KEY: SecretStr = SecretStr("")

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
