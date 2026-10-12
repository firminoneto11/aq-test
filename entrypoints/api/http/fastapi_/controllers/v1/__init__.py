from fastapi import APIRouter

from .probes import router as probes_router

V1_HTTP_ROUTERS: dict[str, APIRouter] = {
    "Probes": probes_router,
}

V1_WS_ROUTERS: dict[str, APIRouter] = {}

V1_SSE_ROUTERS: dict[str, APIRouter] = {}
