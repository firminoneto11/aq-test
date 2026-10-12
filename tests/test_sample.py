import asyncio as aio

from httpx import AsyncClient

from conf import settings
from shared import logging

from .types_ import ReverseURL

logger = logging.get_logger()


async def test_sample():
    await logger.ainfo("Hello from test sample")
    await logger.ainfo(f"{aio.get_running_loop()}")


async def test_hello_world(http_client: AsyncClient, reverse_url: ReverseURL):
    response = await http_client.get(reverse_url("liveness_probe"))
    assert response.status_code == 200
    assert response.json() == {"healthy": True, "commit": settings.COMMIT_ID}
