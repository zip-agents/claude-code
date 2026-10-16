"""Application factory: routers, error handlers and the outer middleware."""

from fastapi import FastAPI, Request, Response
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp

from zip_service.api.docs import DocsCatalog, docs_router
from zip_service.api.health import health_router
from zip_service.api.middleware import RequestContextMiddleware
from zip_service.api.problems import ProblemError, problem_response
from zip_service.api.v1.zip import zip_router
from zip_service.config import Settings
from zip_service.server.context import record_rejection
from zip_service.server.lifecycle import InflightTracker

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


def create_app(
    settings: Settings,
    tracker: InflightTracker | None = None,
    docs: DocsCatalog | None = None,
) -> ASGIApp:
    tracker = tracker or InflightTracker(settings.max_concurrent_requests)
    docs = docs or DocsCatalog.load()
    api = FastAPI(title="zip-service", docs_url=None, redoc_url=None, openapi_url=None)
    api.add_exception_handler(ProblemError, _problem_error_handler)
    api.add_exception_handler(StarletteHTTPException, _http_error_handler)
    api.add_exception_handler(Exception, _internal_error_handler)
    api.include_router(health_router(tracker))
    api.include_router(docs_router(docs))
    api.include_router(zip_router(settings, tracker))
    return RequestContextMiddleware(api)
