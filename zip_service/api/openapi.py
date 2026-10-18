"""The OpenAPI document, generated from the code and served at /docs/openapi.json (§14).

FastAPI generates most of it from the routes and their response models. Two parts are
supplied here, next to the code they describe:

* the zip request body: the route reads the raw request stream (it must never buffer or
  spool the upload), so FastAPI has no parameters to infer it from;
* the error responses: generated from `problems.PROBLEMS`, so the documented codes,
  statuses and titles are exactly the ones the service can send.

The document describes the service as it is reached directly: no server list and no
authentication. A gateway in front of it is expected to add those.
"""

from typing import Any, Literal

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, Field

from zip_service import __version__
from zip_service.api.problems import PROBLEM_MEDIA_TYPE, PROBLEMS, RETRY_AFTER_SECONDS
from zip_service.config import Settings

OPENAPI_PATH = "/docs/openapi.json"
PROBLEM_SCHEMA_REF = "#/components/schemas/Problem"

DESCRIPTION = """\
Zips the files uploaded in one `multipart/form-data` request and streams the archive back
as it is compressed. Upload data is never written to disk; memory per request is bounded
by configuration, not by upload size.

**Streaming.** The `200` status is sent at the first file part. A failure after that
cannot change the status: the connection is closed without the terminating chunk, so the
archive is visibly truncated. Treat a truncated archive as a failed request.

**Part order.** The optional `filename` field only takes effect before the first file
part. Send one `files` part per file.
"""


class Problem(BaseModel):
    """RFC 9457 Problem Details, as every error before streaming begins is reported."""

    type: str = Field(examples=["urn:zip-service:problem:no-files"])
    title: str
    status: int
    detail: str
    code: str = Field(description="Stable machine-readable error code.")
    request_id: str = Field(description="The request's X-Request-ID.")


class LiveStatus(BaseModel):
    status: Literal["ok"]


class ReadyStatus(BaseModel):
    status: Literal["ready", "shutting-down"]


class DocsDocument(BaseModel):
    slug: str
    title: str
    path: str


class DocsManifest(BaseModel):
    service: str
    version: str
    revision: str = Field(description='Git commit the image was built from, or "unknown".')
    contract_version: str
    openapi: str = Field(description="Path of this OpenAPI document.")
    documents: list[DocsDocument]


def problem_responses(*codes: str) -> dict[str, Any]:
    """OpenAPI responses for the given problem codes, grouped by HTTP status."""
    by_status: dict[int, list[str]] = {}
    for code in codes:
        by_status.setdefault(PROBLEMS[code].status, []).append(code)
    responses: dict[str, Any] = {}
    for status, group in sorted(by_status.items()):
        examples = {
            code: {
                "summary": PROBLEMS[code].title,
                "value": {
                    "type": f"urn:zip-service:problem:{code}",
                    "title": PROBLEMS[code].title,
                    "status": status,
                    "detail": PROBLEMS[code].detail,
                    "code": code,
                    "request_id": "3f0c2a5e-0b8e-4c4e-9d55-9a0f6c1e2b7d",
                },
            }
            for code in group
        }
        response: dict[str, Any] = {
            "description": "\n".join(f"- `{code}`: {PROBLEMS[code].detail}" for code in group),
            "headers": {"X-Request-ID": {"$ref": "#/components/headers/X-Request-ID"}},
            "content": {
                PROBLEM_MEDIA_TYPE: {"schema": {"$ref": PROBLEM_SCHEMA_REF}, "examples": examples}
            },
        }
        if "overloaded" in group:
            response["headers"]["Retry-After"] = {
                "description": f"Seconds to wait before retrying (`overloaded` only): "
                f"always {RETRY_AFTER_SECONDS}.",
                "schema": {"type": "integer"},
            }
        responses[str(status)] = response
    return responses


def zip_operation(settings: Settings) -> dict[str, Any]:
    """`openapi_extra` for POST /api/v1/zip: what FastAPI cannot infer from the route."""
    return {
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "filename": {
                                "type": "string",
                                "maxLength": 1024,
                                "description": (
                                    "Archive name; `.zip` is appended if missing. Defaults "
                                    "to `zipped_files.zip`. Must come before the first file."
                                ),
                                "examples": ["quarterly report"],
                            },
                            "files": {
                                "type": "array",
                                "minItems": 1,
                                "maxItems": settings.max_files,
                                "items": {"type": "string", "format": "binary"},
                                "description": (
                                    "One part per file, in archive order. Names are "
                                    "sanitised and de-duplicated, never rejected. "
                                    f"At most {settings.max_files} (MAX_FILES); more aborts "
                                    "the stream with a truncated archive."
                                ),
                            },
                        },
                    },
                    "encoding": {"files": {"contentType": "application/octet-stream"}},
                }
            },
        },
        "responses": {
            "200": {
                "description": "The archive, streamed with chunked transfer encoding "
                "(no Content-Length).",
                "headers": {
                    "Content-Disposition": {
                        "description": "ASCII fallback plus an RFC 8187 `filename*`.",
                        "schema": {"type": "string"},
                        "example": 'attachment; filename="quarterly report.zip"; '
                        "filename*=UTF-8''quarterly%20report.zip",
                    },
                    "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
                    "X-Content-Type-Options": {"schema": {"type": "string", "const": "nosniff"}},
                    "X-Request-ID": {"$ref": "#/components/headers/X-Request-ID"},
                },
                "content": {"application/zip": {"schema": {"type": "string", "format": "binary"}}},
            },
            **problem_responses(
                "malformed-request",
                "no-files",
                "unexpected-file-field",
                "request-timeout",
                # Not "too-many-files": headers go out at the first file part, so MAX_FILES
                # can only be exceeded mid-stream, which aborts (see the README).
                "unsupported-media-type",
                "invalid-archive-name",
                "internal-error",
                "overloaded",
                "shutting-down",
            ),
        },
    }


def install_openapi(api: FastAPI) -> None:
    """Make `api.openapi()` add the shared components FastAPI does not know about."""

    def openapi() -> dict[str, Any]:
        if api.openapi_schema is None:
            schema = get_openapi(
                title="zip-service",
                version=__version__,
                summary="Streaming file-zipping API (contract 1.1.0)",
                description=DESCRIPTION,
                routes=api.routes,
                tags=[
                    {"name": "zip", "description": "Create archives."},
                    {"name": "health", "description": "Liveness and readiness probes."},
                    {"name": "docs", "description": "This service's documentation (§14)."},
                ],
            )
            components = schema.setdefault("components", {})
            components.setdefault("schemas", {})["Problem"] = Problem.model_json_schema()
            components["headers"] = {
                "X-Request-ID": {
                    "description": "The client's X-Request-ID if valid, else a UUIDv4.",
                    "schema": {"type": "string"},
                }
            }
            api.openapi_schema = schema
        return api.openapi_schema

    api.openapi = openapi  # type: ignore[method-assign]
