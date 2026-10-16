# Plan: streaming zip service (Tasks 1–3)

## Context
Take-home brief: build a FastAPI service that zips uploaded files (Task 1), add a Docker image (Task 2) and set up CI/CD (Task 3). The pasted contract (Part 2) is authoritative and will be checked by a black-box conformance suite against the container. Task 4 is out of scope, so no file is created for it.

**Environment constraint:** this container has no python3, uv, unzip or 7z, and no Docker/Podman. I write the code and you run every check on your host (the `/workspace` mount is shared, so `uv.lock` and other generated files show up here). I iterate on the results you report back. I can't run the code myself, so every commit lands before it has been tested. Fixes go in as follow-up commits. If you want each commit to be green, I can fold them back in afterwards with a non-interactive `--autosquash` rebase.

## Assumptions to confirm (reject the plan with corrections if any are wrong)
1. **When headers go out: eagerly.** The 200 and headers are sent as soon as the first non-skipped file part's headers have been parsed, the archive name is valid and its field name is `files`. Any error after that is a mid-stream abort. This matches §2.2 ("if it appears later, it is a mid-stream error"). Consequence: `413 too-many-files` can never happen before streaming begins, because MAX_FILES ≥ 1. The README will document this, and the 413 path will be covered by a unit test only.
2. **CD scope:** pull requests run lint, type checks, fast tests, slow tests and an image build plus container smoke test. Pushes to `main` and `v*` tags also push the image to GHCR, tagged with the commit sha and the semver version. There is no deploy target.
3. **Git identity:** repo-local `user.email=xgenadam@gmail.com`. **I need the name you want on the commits.** Commits end with the Co-Authored-By trailer.

## Stack (and why)
- **Python 3.12** with **uv** and a committed `uv.lock`, which gives reproducible builds and a single tool.
- **FastAPI + uvicorn (httptools)**, running a single process per container. MAX_CONCURRENT_REQUESTS applies per process, and you scale by adding replicas.
- **python-multipart's low-level `MultipartParser`** with callbacks. It streams, is battle-tested, and never writes temp files. I write the `Content-Disposition` parameter parsing myself (including `filename*` per RFC 2231/5987), because python-multipart doesn't decode `filename*`.
- **A custom zip writer of about 150 lines (`zipwriter.py`).** Requirements it covers: Zip64 on *every* entry (in the local header, the data descriptor and the central directory), the UTF-8 flag, and incremental bytes from a push model. stdlib `zipfile` only adds central-directory Zip64 when it is needed. `stream-zip` is pull/generator-based, which is awkward to drive from async input.
- **pydantic-settings** for env and `.env` handling (real env wins) and validation. **ruff** handles lint and format, and **mypy --strict** handles types. Logging is a small stdlib JSON formatter, so no structlog.

## Layout
```
src/zip_service/
  __main__.py    load config (exit 1 + message on error), set up logging, run a uvicorn.Server subclass with custom SIGTERM drain
  config.py      Settings: PORT, LOG_LEVEL, ZIP_COMPRESSION_LEVEL 0-9, SHUTDOWN_GRACE_SECONDS, limits (positive ints)
  logging.py     JSON-lines formatter (timestamp RFC3339 UTC, level, message, request_id via contextvar); uvicorn loggers routed through it; access log off
  problems.py    ProblemError(code), code→(status,title,detail) table, problem response builder
  names.py       archive_name(), sanitize_entry_name(), Deduplicator, content_disposition()
  multipart.py   parse part headers / Content-Disposition params (name, filename, filename*)
  zipwriter.py   sync streaming Zip64 writer: start_entry → bytes, write(chunk) → bytes, end_entry → bytes, finish → bytes
  processor.py   sync: feed(chunk) drives the multipart parser + zip writer + zlib/crc32, returns events/output bytes. Pure and unit-testable
  pipeline.py    async per-request orchestration: reader / writer / watchdog tasks, OutputBuffer, header commit, abort
  lifecycle.py   InflightTracker: immediate-reject concurrency counter, shutting_down flag, register/abort in-flight requests
  middleware.py  pure ASGI (no BaseHTTPMiddleware, which breaks streaming): request ID, X-Request-ID injection, one completion log line per zip request
  app.py         create_app(settings, tracker): routes, 404/405/500 handlers as problem+json
tests/unit/…  tests/integration/… (marked slow)
scripts/smoke_test.sh  Dockerfile  .dockerignore  .gitignore  .env.example  README.md  AGENTS.md
.github/workflows/ci.yml  .github/dependabot.yml
```

## Request flow (POST /api/v1/zip)
1. The route checks, in order: shutting down → `503 shutting-down`; concurrency counter full → `503 overloaded` with `Retry-After: 5`; content type not `multipart/form-data` → 415; no boundary → 400 `malformed-request`. It then takes a slot and returns a `ZipStreamResponse` whose ASGI `__call__` owns receive and send and releases the slot in `finally`.
2. **Reader task:** calls `receive()`, counts `bytes_in`, marks progress, then runs `await to_thread(processor.feed, chunk)`. Multipart parsing, zlib and crc32 all run off the event loop through an anyio CapacityLimiter (zlib and crc32 release the GIL). The processor enforces the field rules (the `filename` field is capped at 1024 bytes, a duplicate is 422, later fields are ignored with a WARNING), the part-header size cap (16 KiB, otherwise malformed), the empty-filename skip, MAX_FILES, and the `files` field name.
3. **Header commit:** on the first file event, the pipeline validates the archive name and sends `http.response.start` with the §3 headers. Errors before that point produce a problem response; errors after it cause an abort.
4. **OutputBuffer:** a byte-bounded async queue. `put` blocks while size ≥ OUTPUT_BUFFER_MAX_BYTES, which applies backpressure: the reader stops calling `receive()`, and uvicorn then pauses socket reads. The **writer task** drains the buffer with `send()`, which blocks while the client isn't reading, counts `bytes_out` and marks progress.
5. **Watchdog:** fires when there has been no progress for IDLE_TIMEOUT_SECONDS, or when MAX_REQUEST_DURATION_SECONDS has passed. Before streaming that means 408; after, an abort.
6. **Disconnect:** `http.disconnect` from `receive()`, including a receive kept outstanding after the body ends, or an OSError from `send()`, cancels the task group and sets outcome `client_disconnected`.
7. **Abort:** cancel the tasks, log at ERROR or WARNING with the code, then raise `StreamAborted`. Uvicorn closes the transport without sending the terminating chunk. A logging filter suppresses uvicorn's duplicate traceback for this exception type. A raw-socket test verifies the behaviour.
8. **Memory:** per request ≈ OUTPUT_BUFFER_MAX_BYTES + one chunk of overshoot + zlib state (~256 KiB) + uvicorn read buffer (~64 KiB) + small parser state. README formula: `RSS ≈ base (~60 MiB) + MAX_CONCURRENT_REQUESTS × (OUTPUT_BUFFER_MAX_BYTES + ~1 MiB)`. A test checks it.

## Shutdown (§9)
uvicorn's own SIGTERM handling closes the listener immediately, so §9's 503 and readiness behaviour would be impossible. I override `handle_exit`. The first signal sets `shutting_down` (readiness and new zip requests return 503) and starts a drain task. The drain waits until the in-flight count reaches 0 or SHUTDOWN_GRACE_SECONDS expires, aborts any requests still running (outcome `aborted`, code `shutting-down`), and then sets `server.should_exit`. A second signal forces the exit.

## Choices where the contract is silent (all go in the README)
- Check order: shutting-down → overloaded → 415 → 400. Health and unknown paths don't count toward concurrency.
- Truncation keeps the dot as part of the extension's ≤16 bytes. If the ` (n)` suffix pushes a name past 255 bytes, the stem is truncated further.
- STORED (level 0) entries still use data descriptors. They are fine for central-directory readers (unzip, 7z, zipfile) but not for strict streaming readers.
- Header bytes are decoded as UTF-8, falling back to latin-1.
- The idle check runs every ~250 ms.
- Non-zip requests get no completion log line (DEBUG only).

## Tests
- **Fast** (`uv run pytest`, which defaults to `-m "not slow"`): names, entry sanitising and de-duplication, `Content-Disposition` parse and format, config validation, zipwriter round-trip through `zipfile` (asserting the Zip64 extra and UTF-8 flag on every entry), OutputBuffer backpressure, a processor fed byte by byte, and the 500 handler and 413 path in-process.
- **Slow** (`uv run pytest -m slow`): runs against a real `python -m zip_service` subprocess on a free port, with env overrides set per test. Clients are httpx plus raw sockets. Covers every item in D2:
  - validity with zipfile, `unzip -t` and `7z t`, checked byte for byte. These skip if the tool is missing, unless `REQUIRE_ZIP_TOOLS=1`, which CI sets
  - RSS sampled from `/proc/<pid>/status` during a ~1 GiB full-duplex upload of incompressible data
  - no disk writes: `/proc/<pid>/io` write_bytes delta, a scan of `/proc/<pid>/fd` for regular files, and an empty TMPDIR
  - half-duplex success; full-duplex with an upload 64× the buffer; stalled clients both before streaming (408) and after (abort timing)
  - every reachable §6.1 code; mid-stream abort (wrong field name, MAX_FILES, broken multipart) with no `0\r\n\r\n` and zipfile rejecting the output
  - disconnects freeing the slot (MAX_CONCURRENT_REQUESTS=1, then the next request succeeds); 503 overloaded with Retry-After; health latency during heavy compression
  - SIGTERM drain, both completing and grace-expired; stdout is valid JSON lines, and completion-line fields are checked

## Container and CI
- **Dockerfile:** two stages, both on a pinned `python:3.12.x-slim-bookworm` tag (the README notes how to pin by digest). The `uv` binary is copied from a pinned `ghcr.io/astral-sh/uv` tag, and `uv sync --locked --no-dev` builds the venv. The runtime stage copies only the venv, runs as non-root uid 10001, precompiles bytecode, and sets `PYTHONDONTWRITEBYTECODE`, so it works with `--read-only`. `HEALTHCHECK` uses Python urllib against `/health/live` on `$PORT`. The command is `CMD ["python","-m","zip_service"]`. `.dockerignore` excludes `.env`, `.git`, tests and caches.
- **`scripts/smoke_test.sh`:** runs the image with `--read-only --cap-drop ALL --network` defaults, waits for the health checks, posts the §13 example, verifies it with `unzip -t`, checks the headers, and tests SIGTERM.
- **ci.yml:** runs on PRs and pushes to main/tags, with concurrency cancel-in-progress. Jobs: `lint` (ruff check, ruff format --check, mypy), `test-fast`, `test-slow` (apt installs unzip and p7zip-full), and `image` (buildx build with GHA cache, then the smoke test, then a GHCR push on main/tags only, needing `packages: write`). `dependabot.yml` covers uv/pip, actions and docker.

## Commit sequence
scaffold (pyproject, tooling, gitignore, .env.example) → config + logging → names → multipart header parsing → zipwriter → processor → pipeline + app + lifecycle → `__main__` / shutdown → integration tests → Dockerfile + smoke test → CI → README + AGENTS.md. Each commit includes its own unit tests.

## Verification (you run these and paste the output back)
```
uv lock && uv sync
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest                      # fast
REQUIRE_ZIP_TOOLS=1 uv run pytest -m slow   # needs unzip + 7z (p7zip-full) on host
docker build -t zip-service .
./scripts/smoke_test.sh zip-service
```

## Changes since the plan was approved
- **Assumptions confirmed by default:** eager header commit (413 is unreachable before
  streaming), GHCR push on main/tags with no deploy job. The commit author name is a
  placeholder (`xgenadam`) until a real name is provided.
- **Forced connection close on abort** (`ClosableHttpToolsProtocol`). uvicorn's
  `transport.close()` waits to flush buffered output, so a non-reading client kept
  aborted connections open. The app now closes gracefully and resets after 1 s if the
  flush can't finish.
- **Everything runs in containers:**
  - `Dockerfile.dev` is a toolbox image (Python, uv, unzip, 7z, curl) built with the
    host UID/GID.
  - The `Makefile` wraps every task (`lock`, `lint`, `format`, `test`, `test-slow`,
    `check`, `image`, `smoke`, `run`, `run-dev`, `shell`, `clean`).
  - `scripts/smoke_test.sh` runs its HTTP and archive checks from the dev image, attached
    to the service container's network, so the host needs only Docker.
  - `.env` is created from `.env.example` and is git-ignored.
- **Layout aimed at an API, not a library** (agreed in review):
  - `main.py` at the repo root is the entrypoint (`python main.py`, also the Docker CMD).
  - `zip_service/api/` mirrors the URLs (`health.py`, `v1/zip.py`) and holds streaming,
    middleware and problems.
  - `zip_service/archive/` holds the synchronous logic (processor, multipart, naming,
    zipwriter).
  - `zip_service/server/` holds the process runtime (server, lifecycle, logging, context).
  - No build system (`[tool.uv] package = false`); the image copies the code next to a
    locked virtualenv.
  - Deliberately not done: a separate domain error type and import-boundary enforcement.
    Not needed while this is only an API.
