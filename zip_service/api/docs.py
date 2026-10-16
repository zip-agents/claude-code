"""GET /docs/manifest.json and GET /docs/<name>.md (contract §14).

The documents are read once, at startup, from a fixed allow-list (`DOCUMENTS`) into
memory. Requests are answered from that dictionary, never by joining a request path onto
the filesystem, so path traversal is impossible by construction. Like the health
endpoints, these routes are not subject to the concurrency limit.
"""

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from zip_service import __version__

CONTRACT_VERSION = "1.1.0"
MAX_DOCUMENT_BYTES = 1024 * 1024
# The project root: README.md and AGENTS.md sit next to main.py, in the repository and in
# the image alike (see Dockerfile).
DOCS_ROOT = Path(__file__).resolve().parents[2]
# (slug, title, file). Order matters: the contract requires readme, then agents.
DOCUMENTS: tuple[tuple[str, str, str], ...] = (
    ("readme", "README", "README.md"),
    ("agents", "AGENTS", "AGENTS.md"),
)

_COMMON_HEADERS = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}
_MARKDOWN = "text/markdown; charset=utf-8"


class DocsError(Exception):
    """A document is missing or too large; raised at startup, never per request."""


@dataclass(frozen=True)
class DocsCatalog:
    manifest: bytes
    files: Mapping[str, bytes]  # "README.md" -> contents

    @classmethod
    def load(cls, root: Path = DOCS_ROOT, revision: str | None = None) -> "DocsCatalog":
        files: dict[str, bytes] = {}
        entries: list[dict[str, str]] = []
        for slug, title, filename in DOCUMENTS:
            path = root / filename
            try:
                content = path.read_bytes()
            except OSError as exc:
                raise DocsError(f"document {filename} is missing from {root}") from exc
            if len(content) > MAX_DOCUMENT_BYTES:
                raise DocsError(f"document {filename} is larger than {MAX_DOCUMENT_BYTES} bytes")
            files[filename] = content
            entries.append({"slug": slug, "title": title, "path": f"/docs/{filename}"})
        manifest = {
            "service": "zip-service",
            "version": __version__,
            "revision": revision or os.environ.get("GIT_SHA") or "unknown",
            "contract_version": CONTRACT_VERSION,
            "documents": entries,
        }
        return cls(json.dumps(manifest).encode(), files)


def docs_router(catalog: DocsCatalog) -> APIRouter:
    router = APIRouter(prefix="/docs")

    @router.get("/manifest.json")
    async def manifest() -> Response:
        return Response(catalog.manifest, media_type="application/json", headers=_COMMON_HEADERS)

    @router.get("/{name}")
    async def document(name: str) -> Response:
        content = catalog.files.get(name)
        if content is None:
            raise StarletteHTTPException(404)  # rendered as a not-found problem (app.py)
        return Response(content, media_type=_MARKDOWN, headers=_COMMON_HEADERS)

    return router
