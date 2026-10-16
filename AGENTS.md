# AGENTS.md

Guidance for AI agents and engineers changing this codebase. The README explains *why*;
this file covers *how to work here safely*. The contract the service implements is the
source of truth for behaviour. A black-box conformance suite runs against the container,
so status codes, problem `code`s, headers and log fields must match it exactly.

## Commands

```bash
make help                 # all targets; everything runs in containers (Docker or ENGINE=podman)
make lock                 # after editing dependencies in pyproject.toml: rewrite uv.lock
make run-dev              # run from source on $PORT from .env
make test                 # fast tests (default excludes `slow`)
make test-slow            # integration tests: real server subprocess, unzip + 7z required
make lint                 # ruff check, ruff format --check, mypy --strict
make format               # auto-format + safe fixes
make check                # lint + all tests (CI gate)
make smoke                # build the production image and smoke-test it
make shell                # shell in the dev container (uv, unzip, 7z, curl available)
```

Without Docker, the uv equivalents work directly: `uv sync`, `uv run pytest [-m slow]`,
`uv run ruff check .`, `uv run mypy`.

After changing dependencies in `pyproject.toml`, run `make lock` and commit `uv.lock`. CI
uses `--locked` and fails on a stale lockfile.

## Architecture in one minute

```
main.py                     entrypoint: load config, configure logging, run GracefulServer
zip_service/app.py          create_app(): include routers, exception handlers → problem+json,
                            wrap in RequestContextMiddleware
zip_service/config.py       Settings (pydantic-settings)
zip_service/api/            HTTP surface; route files mirror URLs
  health.py                 /health/live, /health/ready
  v1/zip.py                 POST /api/v1/zip: shutdown/overload/content-type checks, slot
  streaming.py              ZipPipeline (an ASGI Response): reader / writer / watchdog tasks
                            around a bounded OutputBuffer; problem response vs abort
  middleware.py             outermost ASGI: request ID, last-resort 500, completion log line
  problems.py               ProblemError, PROBLEMS table (code → status/title/detail)
zip_service/archive/        synchronous, no I/O
  processor.py              ArchiveProcessor: multipart callbacks → zip bytes
  zipwriter.py              ZipStreamWriter: Zip64 local headers, descriptors, central dir
  naming.py                 archive/entry name rules, de-duplication, Content-Disposition
  multipart.py              header parameter parsing (Content-Type, Content-Disposition)
zip_service/server/         process runtime
  server.py                 GracefulServer (SIGTERM drain), ClosableHttpToolsProtocol
  lifecycle.py              InflightTracker: concurrency counter, shutdown flag, abort-all
  logging.py, context.py    JSON formatter; request ID / stats contextvars
```

New endpoints go in `zip_service/api/<version>/<resource>.py` as a router factory,
included in `create_app`.

Data flow: `receive()` → `asyncio.to_thread(processor.feed)` → `OutputBuffer.put` →
writer `send()`. Headers are sent by the reader when `processor.streaming` first becomes
true.

## Invariants (never break these)

1. **Never buffer the whole body or touch disk.** Don't use `await request.body()`,
   `request.form()`, `UploadFile`, `tempfile`, or python-multipart's high-level
   `FormParser`/`File`. *Why:* the contract forbids it, and uploads are unbounded.
2. **All per-request memory is bounded by configuration.** Anything that accumulates
   (headers, field values, the central directory) needs a cap or a bound tied to a
   setting. *Why:* the README's memory formula is how instances get sized.
3. **Output only goes through `OutputBuffer`, and `put` must keep blocking when it is
   full.** *Why:* this is the backpressure that prevents duplex deadlock and bounds
   memory (§4.1).
4. **CPU-bound work runs in `asyncio.to_thread`, never on the event loop.** That covers
   parsing, compression and CRC. *Why:* health checks and other requests must stay
   responsive. `test_health_stays_responsive...` guards this.
5. **Headers are sent at the first non-skipped file part, never earlier, and only after
   the archive name has been validated.** *Why:* §5. It decides which errors are problem
   responses and which are aborts.
6. **After headers are sent, failures never complete the response.** No central
   directory, no final `more_body: False` message. The connection is closed through
   `CLOSE_CONNECTION_EXTENSION`. *Why:* §6.2. A truncated archive must be detectably
   broken.
7. **Every response carries `X-Request-ID`, and every error before streaming is
   `application/problem+json`** with a `code` from `problems.PROBLEMS`. Add new codes
   there, never inline. *Why:* the conformance suite checks both.
8. **stdout carries only JSON log lines.** Use `logging`, never `print` (except
   config errors to stderr in `main.py`). *Why:* log ingestion and §10.
9. **Exactly one completion log line per zip request**, emitted by the middleware from
   `RequestStats`. Set `outcome`, `code` and `status` on the stats; don't log a second
   summary. *Why:* §10.
10. **The concurrency slot is released exactly once** (in `ZipPipeline.__call__`'s
    `finally`, or in the route if the pipeline was never created). *Why:* a leak
    permanently shrinks capacity until the service answers only `503 overloaded`.
11. **Every zip entry is Zip64 and the UTF-8 flag is set for non-ASCII names.** Validate
    any writer change with `unzip -t` and `7z t` (slow tests), not just `zipfile`. *Why:*
    §2.5. `zipfile` is lenient.
12. **The image stays non-root, read-only-compatible and offline-capable.** Don't write
    to the filesystem at runtime, and don't fetch anything at startup. *Why:* §11.

## Verifying a change

1. `make lint`
2. `make test`, then `make test-slow`. Any change under `api/` or
   `server/`, or to `archive/zipwriter.py`, **must** pass the slow suite.
3. Container-related changes: `make smoke`.
4. For behaviour changes, check the contract section first, then add a test at the lowest
   level that can express it. Use a pure unit test for rules. Use an integration test
   only for I/O behaviour: timing, sockets, process lifecycle.
5. If the contract is silent and you made a choice, add it to the README's "Choices where
   the contract is silent" section.

## Conventions

- Python 3.12, fully typed (`mypy --strict`), ruff for formatting (line length 100).
- Keep pure logic (names, parsing, zip format) free of I/O and asyncio so it is
  unit-testable. Keep the async layer thin.
- Raise `ProblemError(code, detail)` for contract errors. `detail` is user-facing: never
  put internals, paths or exception text in it.
- Log with `extra={"fields": {...}}` for structured fields. The request ID is added
  automatically from the contextvar, including inside worker threads, because `to_thread`
  copies context.
- Tests: `tests/unit` must be fast and need no network or subprocesses. `tests/integration`
  modules set `pytestmark = pytest.mark.slow` and use the `server`/`make_server` fixtures.
  Build bodies with `tests/multipart_builder.py`, and use `RawClient` when you need control
  over socket reads and writes.
- Commits: small, one logical change each, imperative subject line, tests passing.
- Configuration: add new settings to `config.Settings`, `.env.example` (a test enforces
  this), the README table, and the integration `SETTING_NAMES` set.
