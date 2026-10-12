from dataclasses import dataclass
from typing import cast

from fastapi import FastAPI

from conf import settings

from .controllers.v1 import V1_HTTP_ROUTERS, V1_SSE_ROUTERS, V1_WS_ROUTERS
from .types_ import AsgiApp


@dataclass
class ApplicationMount:
    path: str
    app: AsgiApp
    name: str


def _get_app_v1():
    v1_app = FastAPI(**settings.get_asgi_settings(main_mount=False))  # type: ignore

    for name, router in V1_HTTP_ROUTERS.items():
        v1_app.include_router(router, tags=[name])

    for name, router in V1_SSE_ROUTERS.items():
        v1_app.include_router(router, tags=[name], prefix="/sse")

    for name, router in V1_WS_ROUTERS.items():
        v1_app.include_router(router, tags=[name], prefix="/ws")

    return cast(AsgiApp, v1_app)


def get_mounts():
    return [
        ApplicationMount(
            path=f"{settings.API_PREFIX}/v1", app=_get_app_v1(), name="v1"
        ),
    ]
