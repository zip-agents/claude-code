"""RFC 9457 problem details for every error the service reports (contract §6.1)."""

from collections.abc import Mapping
from dataclasses import dataclass

from fastapi.responses import JSONResponse

from zip_service.server.context import request_id_var

PROBLEM_MEDIA_TYPE = "application/problem+json"
RETRY_AFTER_SECONDS = "5"


@dataclass(frozen=True)
class ProblemSpec:
    status: int
    title: str
    detail: str


PROBLEMS: dict[str, ProblemSpec] = {
    "malformed-request": ProblemSpec(
        400, "Malformed request", "The request body is not valid multipart/form-data."
    ),
    "no-files": ProblemSpec(
        400,
        "No files were uploaded",
        "The request contained no file parts with a non-empty filename.",
    ),
    "unexpected-file-field": ProblemSpec(
        400, "Unexpected file field", "File parts must use the field name 'files'."
    ),
    "not-found": ProblemSpec(404, "Not found", "No resource exists at this path."),
    "method-not-allowed": ProblemSpec(
        405, "Method not allowed", "The method is not allowed for this path."
    ),
    "request-timeout": ProblemSpec(
        408, "Request timeout", "The request did not make progress within the time limit."
    ),
    "too-many-files": ProblemSpec(
        413, "Too many files", "The request contained more file parts than are allowed."
    ),
    "unsupported-media-type": ProblemSpec(
        415, "Unsupported media type", "The request Content-Type must be multipart/form-data."
    ),
    "invalid-archive-name": ProblemSpec(
        422, "Invalid archive name", "The requested archive name is not allowed."
    ),
    "internal-error": ProblemSpec(
        500, "Internal server error", "An unexpected error occurred while handling the request."
    ),
    "overloaded": ProblemSpec(
        503, "Service overloaded", "Too many requests are in progress. Retry later."
    ),
    "shutting-down": ProblemSpec(
        503, "Service shutting down", "The service is shutting down. Retry the request."
    ),
}


class ProblemError(Exception):
    """An error that maps to one of the contract's problem codes."""

    def __init__(self, code: str, detail: str | None = None) -> None:
        if code not in PROBLEMS:
            raise ValueError(f"unknown problem code: {code}")
        super().__init__(detail or PROBLEMS[code].detail)
        self.code = code
        self.detail = detail or PROBLEMS[code].detail
        self.status = PROBLEMS[code].status


def problem_response(
    code: str, detail: str | None = None, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    spec = PROBLEMS[code]
    body = {
        "type": f"urn:zip-service:problem:{code}",
        "title": spec.title,
        "status": spec.status,
        "detail": detail or spec.detail,
        "code": code,
        "request_id": request_id_var.get(),
    }
    response_headers = dict(headers or {})
    if code == "overloaded":
        response_headers["Retry-After"] = RETRY_AFTER_SECONDS
    return JSONResponse(
        body, status_code=spec.status, headers=response_headers, media_type=PROBLEM_MEDIA_TYPE
    )
