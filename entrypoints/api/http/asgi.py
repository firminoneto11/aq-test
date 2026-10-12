from typing import TYPE_CHECKING, cast

from conf import settings

if TYPE_CHECKING:
    from uvicorn._types import ASGIApplication

    from conf._types import HTTPBackendFrameworkTypes


def create_app_for(framework: HTTPBackendFrameworkTypes):
    match framework:
        case "fastapi":
            from .fastapi_.factory import ASGIFactory

    _, app = ASGIFactory.new()
    return cast("ASGIApplication", app)


app = create_app_for(framework=settings.HTTP_BACKEND_FRAMEWORK)
