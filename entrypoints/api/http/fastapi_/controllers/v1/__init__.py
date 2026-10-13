from fastapi import APIRouter

from .auth import router as auth_router
from .interviews import router as interviews_router
from .jobs import router as jobs_router
from .probes import router as probes_router

V1_HTTP_ROUTERS: dict[str, APIRouter] = {
    "Auth": auth_router,
    "Interviews": interviews_router,
    "Jobs": jobs_router,
    "Probes": probes_router,
}

V1_WS_ROUTERS: dict[str, APIRouter] = {}

V1_SSE_ROUTERS: dict[str, APIRouter] = {}
