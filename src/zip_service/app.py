"""Application factory: routes, error handlers and middleware."""

from datetime import UTC, datetime

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp

from zip_service.config import Settings
from zip_service.context import current_stats, record_rejection
from zip_service.lifecycle import InflightTracker
from zip_service.middleware import ZIP_PATH, RequestContextMiddleware
from zip_service.multipart import ContentTypeError, multipart_boundary
from zip_service.pipeline import ZipPipeline
from zip_service.problems import ProblemError, problem_response

_HTTP_ERROR_CODES = {404: "not-found", 405: "method-not-allowed"}


async def _problem_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, ProblemError)
    record_rejection(exc.code)
    return problem_response(exc.code, exc.detail)


async def _http_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, StarletteHTTPException)
    code = _HTTP_ERROR_CODES.get(exc.status_code)
    if code is None:  # not raised by this service's routes; keep the framework's status
        response = problem_response("internal-error", headers=exc.headers)
        response.status_code = exc.status_code
        return response
    return problem_response(code, headers=exc.headers)


async def _internal_error_handler(request: Request, exc: Exception) -> Response:
    # Never echo the exception: details stay in the logs (RequestContextMiddleware).
    return problem_response("internal-error")


def create_app(settings: Settings, tracker: InflightTracker | None = None) -> ASGIApp:
    tracker = tracker or InflightTracker(settings.max_concurrent_requests)
    api = FastAPI(title="zip-service", docs_url=None, redoc_url=None, openapi_url=None)
    api.add_exception_handler(ProblemError, _problem_error_handler)
    api.add_exception_handler(StarletteHTTPException, _http_error_handler)
    api.add_exception_handler(Exception, _internal_error_handler)

    @api.get("/health/live")
    async def live() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    @api.get("/health/ready")
    async def ready() -> JSONResponse:
        if tracker.shutting_down:
            return JSONResponse({"status": "shutting-down"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @api.post(ZIP_PATH)
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

    return RequestContextMiddleware(api)
