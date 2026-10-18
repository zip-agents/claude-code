"""POST /api/v1/zip: admission checks, then hand the request to the streaming pipeline."""

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response

from zip_service.api.openapi import zip_operation
from zip_service.api.problems import ProblemError
from zip_service.api.streaming import ZipPipeline
from zip_service.archive.multipart import ContentTypeError, multipart_boundary
from zip_service.config import Settings
from zip_service.server.context import current_stats
from zip_service.server.lifecycle import InflightTracker

ZIP_PATH = "/api/v1/zip"


def zip_router(settings: Settings, tracker: InflightTracker) -> APIRouter:
    router = APIRouter()

    @router.post(
        ZIP_PATH,
        tags=["zip"],
        summary="Zip uploaded files",
        # A plain Response: the archive is streamed, never a JSON body.
        response_class=Response,
        openapi_extra=zip_operation(settings),
    )
    async def create_zip(request: Request) -> Response:
        received_at = datetime.now(UTC)
        if tracker.shutting_down:
            raise ProblemError("shutting-down")
        if not tracker.try_acquire():
            raise ProblemError("overloaded")
        try:
            boundary = multipart_boundary(request.headers.get("content-type"))
            pipeline = ZipPipeline(
                boundary=boundary,
                settings=settings,
                tracker=tracker,
                stats=current_stats(),
                received_at=received_at,
            )
        except BaseException as exc:
            tracker.release()
            if isinstance(exc, ContentTypeError):
                raise ProblemError(exc.code, str(exc)) from None
            raise
        # From here the pipeline owns the slot and releases it when the response ends.
        tracker.track(pipeline)
        return pipeline

    return router
