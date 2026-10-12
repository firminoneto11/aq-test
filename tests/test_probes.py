from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from httpx import AsyncClient

from conf import settings
from entrypoints.api.http.fastapi_.factory import ASGIFactory

from .types_ import ReverseURL

if TYPE_CHECKING:
    from entrypoints.api.http.fastapi_.types_ import AsgiApp


async def test_liveness_probe(http_client: AsyncClient, reverse_url: ReverseURL):
    response = await http_client.get(reverse_url("liveness_probe"))
    assert response.status_code == 200
    assert response.json() == {"healthy": True, "commit": settings.COMMIT_ID}


async def test_readiness_probe_healthy(
    http_client: AsyncClient, reverse_url: ReverseURL
):
    response = await http_client.get(reverse_url("readiness_probe"))
    assert response.status_code == 200
    data = response.json()
    assert data["is_up"] is True
    assert data["is_database_connected"] is True
    assert data["environment"] == settings.ENVIRONMENT
    assert data["commit_id"] == settings.COMMIT_ID


async def test_readiness_probe_db_down(
    http_client: AsyncClient,
    reverse_url: ReverseURL,
    asgi_config: tuple[ASGIFactory, AsgiApp],
):
    _, main_app = asgi_config
    with patch.object(
        main_app.state.db,
        "ping",
        new=AsyncMock(side_effect=Exception("DB unreachable")),
    ):
        response = await http_client.get(reverse_url("readiness_probe"))
    assert response.status_code == 503
    data = response.json()
    assert data["is_up"] is False
    assert data["is_database_connected"] is False
