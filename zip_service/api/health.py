"""GET /health/live and GET /health/ready (contract §8). Not subject to the concurrency limit."""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from zip_service.api.openapi import LiveStatus, ReadyStatus
from zip_service.server.lifecycle import InflightTracker


def health_router(tracker: InflightTracker) -> APIRouter:
    router = APIRouter(prefix="/health", tags=["health"])

    @router.get("/live", summary="Liveness", response_model=LiveStatus)
    async def live() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @router.get(
        "/ready",
        summary="Readiness",
        response_model=ReadyStatus,
        responses={503: {"model": ReadyStatus, "description": "Shutting down."}},
    )
    async def ready() -> JSONResponse:
        if tracker.shutting_down:
            return JSONResponse({"status": "shutting-down"}, status_code=503)
        return JSONResponse({"status": "ready"})

    return router
