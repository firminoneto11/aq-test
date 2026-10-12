import asyncio

from fastapi import APIRouter, Response

from conf import settings
from entrypoints.api.http.fastapi_.utils import DBClientDep

router = APIRouter(prefix="/probes")

_DB_PROBE_TIMEOUT_S = 2.0


@router.get("/health", summary="Liveness probe")
async def liveness_probe():
    return {"healthy": True, "commit": settings.COMMIT_ID}


@router.get("/ready", summary="Readiness probe")
async def readiness_probe(db_client: DBClientDep, response: Response):
    try:
        await asyncio.wait_for(db_client.ping(), timeout=_DB_PROBE_TIMEOUT_S)
        is_db_client_up = True
    except Exception:  # noqa
        is_db_client_up = False

    if not is_db_client_up:
        response.status_code = 503

    return {
        "app_name": settings.APP_NAME,
        "environment": settings.ENVIRONMENT,
        "version": settings.VERSION,
        "branch": settings.BRANCH_NAME,
        "commit_id": settings.COMMIT_ID,
        "is_database_connected": is_db_client_up,
        "is_up": is_db_client_up,
    }
