# zip-service

An HTTP API that takes several uploaded files in one `multipart/form-data` request and
returns a ZIP archive containing them. The archive is **streamed**: each file is compressed
as its bytes arrive and output is sent as soon as it exists. Upload data is never written
to disk, and memory per request is bounded by configuration, not by upload size.

This is a proof of concept that serves real traffic and is meant to grow. The goal is a
small codebase that is easy to reason about, where each operational choice is written
down here.

- [Running it](#running-it)
- [API](#api)
- [Design](#design)
- [Memory budget](#memory-budget)
- [Choices where the contract is silent](#choices-where-the-contract-is-silent)
- [Testing](#testing)
- [CI/CD](#cicd)
- [Known limitations](#known-limitations)
- [With more time](#with-more-time)

## Running it

Everything runs in containers. The host needs only Docker (or `ENGINE=podman`) and
`make`. `make help` lists the targets.

```bash
cp .env.example .env     # local settings for `make run` / `make run-dev`; never committed
make lock                # resolve dependencies into uv.lock (commit it)
make run-dev             # run from source in the dev container on $PORT (default 8000)
make run                 # build and run the production image with .env
```

```bash
curl -sS -o out.zip -D - \
  -F 'filename=quarterly report' \
  -F 'files=@a.txt' -F 'files=@a.txt' -F 'files=@photo.jpg' \
  http://localhost:8000/api/v1/zip
```

`Dockerfile.dev` is the toolbox image: Python 3.12, uv, `unzip`, `7z` and `curl`. It is
built with your UID/GID, so files written to the bind-mounted repository stay yours. The
virtualenv and uv cache live in named volumes (`make clean` removes them). Without
Docker, the same commands work directly with uv (`uv sync`, `uv run pytest`, ...).

### Tests and checks

```bash
make test                # fast: unit + in-process tests (~seconds)
make test-slow           # slow: integration tests against a real server process
make lint                # ruff check, ruff format --check, mypy --strict
make format              # apply ruff formatting and safe fixes
make check               # lint + all tests: what CI runs before building the image
make coverage            # all tests under coverage: report + coverage.xml
make coverage-diff       # lines changed since main that no test executes
make test ARGS='-k names -x'   # pass extra pytest arguments
```

The dev container sets `REQUIRE_ZIP_TOOLS=1`, so a missing `unzip` or `7z` fails the
slow suite instead of skipping it.

### Container

```bash
make image               # docker build -t zip-service:local .
make smoke               # build, then smoke-test the image hardened
docker run --rm -p 8000:8000 --read-only --cap-drop ALL zip-service:local
```

The smoke test runs the image the way production should: read-only root filesystem, no
capabilities, `no-new-privileges`. It checks the contract example end to end (`unzip -t`,
`7z t`, byte for byte), headers, health checks, JSON logs, `PORT`, operation with
`--network none`, the `HEALTHCHECK`, exit code 0 after SIGTERM, and that invalid
configuration exits non-zero. Its HTTP and archive checks run in the dev image, attached
to the service container's network, so the host needs no tools besides Docker.

### Configuration

All settings are environment variables, validated at startup. Invalid values make the
process exit with status 2 and a message naming each bad variable (on stderr, because
stdout is reserved for JSON logs). In development a `.env` file in the working directory
is loaded too. Real environment variables win, and every setting has a default, so no
`.env` is needed. See [`.env.example`](.env.example) for the full list with defaults.

| Variable | Default | |
|---|---|---|
| `PORT` | `8000` | listens on `0.0.0.0` |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` (case-insensitive) |
| `ZIP_COMPRESSION_LEVEL` | `6` | `0` = STORED, `1`–`9` = DEFLATE level |
| `SHUTDOWN_GRACE_SECONDS` | `25` | ≥ 0 |
| `MAX_FILES` | `500` | |
| `MAX_CONCURRENT_REQUESTS` | `32` | per process |
| `IDLE_TIMEOUT_SECONDS` | `30` | |
| `MAX_REQUEST_DURATION_SECONDS` | `900` | |
| `OUTPUT_BUFFER_MAX_BYTES` | `16777216` | |

## API

`POST /api/v1/zip` with `multipart/form-data`:

- an optional `filename` field (the archive name), which must come before the first file;
- one or more `files` parts.

On success the response is `200 application/zip`, sent with chunked transfer encoding. It
carries `Content-Disposition` (an ASCII fallback plus an RFC 8187 `filename*`),
`Cache-Control: no-store` and `X-Content-Type-Options: nosniff`. Errors are RFC 9457
`application/problem+json` responses with a stable `code`. Every response carries
`X-Request-ID`.

`GET /health/live` returns 200 whenever the process is running. `GET /health/ready`
returns 200, or 503 while the service is shutting down. Health endpoints don't count
toward the concurrency limit.

## Design

```
            ┌──────────────────── one zip request ─────────────────────┐
 socket ──► │ reader task ──► worker thread ──► OutputBuffer ──► writer │ ──► socket
            │  receive()      ArchiveProcessor   (bounded,       task   │
            │                 multipart → zip    byte-counted)   send() │
            │                 DEFLATE, CRC-32                            │
            │ watchdog task: idle + total-duration timeouts             │
            └───────────────────────────────────────────────────────────┘
```

The layout follows the API. `main.py` at the repository root is the entrypoint, and
routes live in files that mirror their URLs.

```
main.py                    entrypoint: config → logging → server (python main.py)
zip_service/
  app.py                   create_app(): routers, error handlers, outer middleware
  config.py                settings
  api/                     the HTTP surface
    health.py              GET /health/live, GET /health/ready
    v1/zip.py              POST /api/v1/zip: admission checks, hands off to streaming
    streaming.py           ZipPipeline + OutputBuffer: reader/writer/watchdog tasks,
                           when headers go out, problem response vs mid-stream abort
    middleware.py          request IDs, last-resort 500, one completion log line
    problems.py            problem+json responses and the code → status table
  archive/                 synchronous, no I/O
    processor.py           multipart bytes → zip bytes (via stdlib zipfile), enforces the
                           part rules (§2.2)
    multipart.py           header-parameter parsing (Content-Type, Content-Disposition)
    naming.py              archive/entry name rules, de-duplication, Content-Disposition
  server/                  the process
    server.py              GracefulServer (SIGTERM drain), ClosableHttpToolsProtocol
    lifecycle.py           concurrency counter, shutdown flag
    logging.py             JSON-lines formatter
    context.py             request ID and per-request stats contextvars
```

The rules live in `archive/` as plain synchronous code, so they are unit-tested
exhaustively without a server. The async layer is thin and tested against a real process.

There is no build step: `pyproject.toml` declares dependencies with
`[tool.uv] package = false`, and the image copies `main.py` and `zip_service/` next to a
locked virtualenv. The artifact is the Docker image, and `make smoke` tests it.

### Streaming

- **Input.** The endpoint is a FastAPI route that returns a custom ASGI `Response`. That
  response calls `receive()` itself, so Starlette never buffers the body, and there are
  no temp files (FastAPI's `UploadFile` would spool to disk).
- **Processing.** Each body chunk goes to `ArchiveProcessor.feed()` in a worker thread
  (`asyncio.to_thread`). Multipart scanning, DEFLATE and CRC-32 are CPU-bound. zlib and
  `crc32` release the GIL, and the event loop stays free for other requests and health
  checks. A test checks health-check latency during level-9 compression.
- **Output.** Every entry is written with a data descriptor because sizes are unknown up
  front. Output goes into the `OutputBuffer`, and the writer task sends it as chunked
  transfer encoding.

### Duplex behaviour and the output buffer (contract §4.1)

Many HTTP clients (curl, `requests`, `httpx`, browsers) don't read the response
until they have finished sending the request. If the service kept writing
output that nobody reads while it waits for more input, both sides would block forever.

So compressed output is buffered in memory, up to `OUTPUT_BUFFER_MAX_BYTES`. When the
buffer is full, `put()` blocks the reader task. The reader stops calling `receive()`,
uvicorn stops reading the socket, and TCP flow control pushes back on the client. Memory
stays bounded, and nothing is dropped.

Each request has a watchdog. "Progress" means a body chunk was received or a response
chunk was handed to the server. If neither happens for `IDLE_TIMEOUT_SECONDS`, the request
is aborted (or gets `408` if streaming has not begun). The same applies once
`MAX_REQUEST_DURATION_SECONDS` has passed.

What this means for clients:

- **Half-duplex clients** (send everything, then read) succeed **if the output produced
  before the upload ends fits in `OUTPUT_BUFFER_MAX_BYTES`**. Socket buffers add a little
  headroom, but don't rely on it. With highly compressible input the limit is far above
  16 MiB of upload. With already-compressed input (JPEGs, video, zips) it is about 16 MiB.
  Beyond that, the request stalls and is aborted after the idle timeout.
- **Full-duplex clients** (clients that read the response while still sending, which
  usually means a purpose-built client) succeed at any size within the time limits.

Raising `OUTPUT_BUFFER_MAX_BYTES` lets bigger half-duplex uploads succeed, at a direct
memory cost per concurrent request (see the [memory budget](#memory-budget)).

### When streaming begins, and failures after that

The status line and headers are sent when the first non-skipped file part's headers have
been parsed. By then the archive name has been validated, and the part's field name has
been checked. Any error before that point is a normal problem response.

After that point, any failure (MAX_FILES, malformed multipart, a later file part with the
wrong field name, a timeout, an internal error or shutdown) is handled per §6.2. The
service stops sending, never writes the central directory or the terminating zero-length
chunk, and closes the connection. The client gets a truncated archive that unzip tools
reject. The failure is logged with its `code`, at WARNING if the client caused it and
ERROR otherwise.

Closing reliably needed one uvicorn customisation. When an ASGI app returns mid-response,
uvicorn calls `transport.close()`, which waits to flush buffered output. A client that is
not reading never lets that flush finish, so the connection would stay open forever.
`ClosableHttpToolsProtocol` adds an ASGI scope extension that closes gracefully but aborts
(RST) the transport if the flush hasn't completed within one second.

If the client disconnects, the next `receive()` returns `http.disconnect`, or `send()`
fails. All three tasks are cancelled, the concurrency slot is released, and the request
is logged as `client_disconnected`.

### Concurrency limit

`InflightTracker` is a counter on the event loop. A request over `MAX_CONCURRENT_REQUESTS`
gets `503 overloaded` with `Retry-After: 5` immediately. It is never queued, because
queueing would hold memory and sockets for work that might never run. The slot belongs to
the response object and is released in its `finally` block, so every exit path releases
it.

### Graceful shutdown (contract §9)

uvicorn's own SIGTERM handling stops listening at once, so new requests would get
connection errors instead of `503 shutting-down`, and readiness could not report 503.
`GracefulServer.handle_exit` replaces it. The first SIGTERM/SIGINT sets `shutting_down`:
readiness returns 503 and new zip requests get `503 shutting-down`, while the server keeps
listening. In-flight requests get `SHUTDOWN_GRACE_SECONDS` to finish. Anything still
running after that is aborted (code `shutting-down`), and the process exits with
status 0. A second signal exits immediately.

### Observability

Logs go to stdout as JSON lines: `timestamp` (RFC 3339 UTC), `level`, `message`, `logger`,
and `request_id` within a request. uvicorn's own logs go through the same formatter, and
its access log is disabled. Each `POST /api/v1/zip` emits exactly one completion line with
`method`, `path`, `status`, `duration_ms`, `bytes_in`, `bytes_out`, `files_count`,
`outcome` and, for non-`completed` outcomes, `code`.

### Libraries

| Choice | Why |
|---|---|
| FastAPI + uvicorn (httptools) | FastAPI is required by the brief. uvicorn supports reading the body and writing the response concurrently, which duplex streaming needs, and applies transport-level flow control. |
| python-multipart (low-level `MultipartParser`) | An incremental, callback-based parser that is already FastAPI's dependency and widely used. Its high-level form parser spools files to disk, so it is not used. |
| Stdlib `zipfile` for the archive format | Driven incrementally through a small non-seekable sink: each `write()` returns the zip bytes produced so far, which fits the push model (bytes arrive from the socket). A non-seekable output makes it stream with data descriptors. `force_zip64=True` on every entry reserves 64-bit sizes, because sizes are unknown up front, and zipfile writes Zip64 central-directory and end records when a value needs them. No dependency, maintained by CPython, familiar to every Python developer. `stream-zip` and `zipstream-ng` are pull-based (they iterate generators), which would need a thread and queues to bridge from the parser callbacks. |
| Own header-parameter parsing | python-multipart doesn't decode `filename*` and rewrites some backslash paths. |
| pydantic-settings | Typed validation, `.env` support and env-over-dotenv precedence, with no custom code. |
| uv | One tool for Python versions, locking and running. `uv.lock` makes local, CI and image builds use the same dependencies. |
| ruff, mypy `--strict` | Fast lint and format. Strict typing keeps the contract's edge cases honest as the team grows. |

## Memory budget

Per in-flight zip request:

| Item | Bound |
|---|---|
| Output buffer | `OUTPUT_BUFFER_MAX_BYTES` + one chunk (output of one input chunk, ≤ ~256 KiB) |
| zlib compressor state | ~270 KiB at the default `memLevel` |
| uvicorn read-ahead before pausing the socket | ~64–256 KiB |
| Multipart parser, part headers (capped at 16 KiB), `filename` field (≤ 1 KiB) | < 50 KiB |
| Central directory kept until the end (one `ZipInfo` per entry) | ~1 KiB × `MAX_FILES` (≈ 0.5 MiB at 500) |
| Asyncio transport write buffer | ≤ 64 KiB + one chunk |

So, per instance:

```
RSS ≈ B + MAX_CONCURRENT_REQUESTS × (OUTPUT_BUFFER_MAX_BYTES + C)

B ≈ 60 MiB   interpreter, FastAPI/uvicorn, worker thread stacks
C ≈ 1.5 MiB  the per-request overhead above (with headroom)
```

With the defaults that is 60 MiB + 32 × 17.5 MiB ≈ **620 MiB**, so a container memory limit
of about 768 MiB leaves headroom. The integration test `test_memory_stays_bounded...` streams
256 MiB of incompressible data (set `MEMORY_TEST_UPLOAD_MIB` to go larger) with a 4 MiB
buffer. It pauses the reader so the buffer fills completely, and asserts that RSS grows by
less than the buffer plus 40 MiB.

## Choices where the contract is silent

- **`413 too-many-files` is never sent.** Headers go out at the first file part, and
  `MAX_FILES` ≥ 1, so the limit can only be exceeded mid-stream (abort, code
  `too-many-files`). Delaying headers to make 413 reachable would make streaming start
  depend on the data. The 413 path is covered by unit tests.
- **Check order** for a zip request: shutting down → concurrency limit → `Content-Type`
  (415) → boundary (400) → body.
- **Archive name trimming.** Control characters are checked *before* trimming, so a
  trailing `\n` is rejected rather than silently trimmed. The trimmed name is what gets
  used. A `filename` field that isn't valid UTF-8 → `422 invalid-archive-name`.
- **Archive name errors are reported where they occur.** An invalid, duplicate or
  oversized `filename` field returns 422 at that field, even if the body later turns out
  to have no files.
- **Empty or non-multipart bodies.** An empty body, or one without a closing boundary,
  returns `400 malformed-request` (not `no-files`), because it is not valid multipart.
  Preamble text before the first boundary is also rejected as malformed.
- **Parts without `Content-Disposition`** → `malformed-request`. A file part without a
  `name` counts as a wrong field name (`unexpected-file-field`).
- **Part header size** is capped at 16 KiB per part (`malformed-request`), because headers
  are held in memory.
- **Header decoding.** Part header bytes are decoded as UTF-8 (what browsers send), falling
  back to Latin-1. In quoted parameters only `\"` and `\\` are unescaped, so unescaped
  Windows paths (`C:\dir\a.txt`) still reduce to `a.txt`. `filename*` supports UTF-8,
  ISO-8859-1 and US-ASCII; other charsets are decoded as UTF-8 with replacement.
  RFC 2231 continuations (`filename*0*`) are not supported.
- **A part with only `filename*`** is a file part.
- **Entry name truncation.** The ≤16-byte extension limit counts the dot. If adding
  ` (n)` would push a name past 255 bytes, the stem is truncated further.
- **Timestamps.** DOS timestamps have no time zone and 2-second resolution; the UTC wall
  time is stored. No extended-timestamp extra field is written.
- **Zip64 ("every entry MUST be written with Zip64 extensions")** is read as its purpose:
  no 4 GiB limit. Every entry reserves 64-bit sizes (Zip64 local-header extra and data
  descriptor). The central directory and end records use Zip64 only when a value exceeds
  the 32-bit limit, which is stdlib `zipfile`'s behaviour and what readers rely on.
- **STORED entries (level 0)** still use data descriptors. Central-directory readers
  (unzip, 7z, zipfile, OS archive tools) handle this. Strict streaming readers such as
  Java's `ZipInputStream` don't support STORED with data descriptors.
- **Status in logs.** For a client that disconnects before any response, the completion
  line records status `499` (nginx convention) and code `client-disconnected`. For aborts
  it records `200`, the status that was sent.
- **Completion lines** are only emitted for `POST /api/v1/zip`. Other requests log at
  DEBUG. The level of a completion line is INFO for `completed`/`rejected`, WARNING for
  client-caused aborts and disconnects, and ERROR for internal errors and
  shutdown aborts.
- **Error responses before streaming** don't close the connection; uvicorn discards the
  rest of the body. This avoids TCP resets that can destroy the error response before the
  client reads it. The exception is `408`, which sends `Connection: close` because the
  client has stopped sending.
- **After an abort**, bytes already handed to the server get one second to flush before
  the connection is reset.
- **Unknown HTTP errors** from the framework (none are known to occur) keep their status
  and use the `internal-error` problem body.
- **`/docs` and `/openapi.json` are disabled.** The contract defines every other path as
  404. Re-enable them in `create_app` if wanted.
- **One process per container.** `MAX_CONCURRENT_REQUESTS` applies per process, and you
  scale horizontally with replicas. No worker processes.
- **`SHUTDOWN_GRACE_SECONDS`** may be `0` (abort in-flight requests immediately). `PORT`
  must be 1–65535.
- **Exit status** is 0 after a graceful shutdown and 2 on invalid configuration.

## Testing

| Suite | Command | What it covers |
|---|---|---|
| Fast | `uv run pytest` | Name rules, sanitisation and de-duplication, `Content-Disposition`, header parsing, configuration validation, the processor (archives round-tripped through `zipfile`, Zip64 local headers, UTF-8 flags and compression methods inspected) fed in many chunkings (1 byte to whole body) including every error, output-buffer backpressure, and in-process routing and error handling (`TestClient`). |
| Slow | `uv run pytest -m slow` | A real `python main.py` subprocess, driven by `httpx` and a raw-socket HTTP/1.1 client. Covers end-to-end streaming (archive bytes for a file reach the client while that file is still uploading), archive validity with `zipfile`, `unzip -t` and `7z t` with contents compared byte for byte, bounded RSS during a large upload, no disk writes, half-duplex success, full-duplex success at 128× the buffer, stalled clients aborted, every reachable §6.1 code, mid-stream truncation without the terminating chunk, disconnects releasing the concurrency slot, `503 overloaded`, health latency under load, and SIGTERM draining and aborting. |
| Container | `scripts/smoke_test.sh` | The built image, hardened (see above). |

**Archive tools tested against:** Python `zipfile` (3.12), Info-ZIP UnZip 6.00 and the
`7z` from the `unzip` and `p7zip-full` packages on Ubuntu 24.04 (CI). Every entry reserves 64-bit sizes
(a Zip64 extra in its local header and a Zip64 data descriptor). The central directory
and end records switch to Zip64 when an entry or the archive exceeds 4 GiB. Very old tools
without Zip64 support (UnZip < 6.0, Windows XP's built-in extractor) can't read those.

No-disk-writes is checked three ways: no regular file is held open for writing during a
128 MiB upload, the server's working directory (also its `TMPDIR`) stays empty, and
`/proc/<pid>/io` `write_bytes` grows by less than 1 MiB. The container smoke test adds a
read-only root filesystem.

### Coverage

Coverage is **informational: it never fails a build.** `make coverage` (and CI) runs every
test under coverage.py, line coverage only (`COVERAGE_CORE=sysmon`, which keeps tracing
overhead low on Python 3.12). That includes the server subprocesses the integration tests
start (`[tool.coverage.run] patch = ["subprocess"]`), so the streaming and shutdown code
counts.

On pull requests, [diff-cover](https://github.com/Bachmann1234/diff_cover) compares the
coverage data with the diff against the base branch. Every block of changed lines that no
test executes becomes a warning annotation on the PR, and the job summary shows the total
and the changed-line report. Locally, run `make coverage` then `make coverage-diff`
(`COMPARE_BRANCH=origin/main` once there is a remote).

The checks look at *new uncovered code* rather than comparing the total with `main`.
Totals move for reasons that don't matter (deleted well-tested code lowers them), and a
baseline comparison would need main's results stored between runs. Uncovered changed lines
are what a reviewer can act on.

## CI/CD

`.github/workflows/ci.yml` runs on every pull request and on pushes to `main` and `v*`
tags. Superseded PR runs are cancelled.

| Job | Purpose |
|---|---|
| `lint` | `ruff check`, `ruff format --check` and `mypy --strict`. Catches style drift and type errors in review instead of in production, so reviewers can focus on behaviour. `uv sync --locked` also fails if `uv.lock` is stale. |
| `test-fast` | Unit and in-process tests, which give the quickest signal on rule changes. |
| `test-slow` | All tests (integration and unit) under coverage, against a real server, with `unzip` and `7z` installed and **required**. This is where streaming, duplex, abort and shutdown regressions show up. It adds the coverage report to the job summary, and on pull requests it adds warning annotations for changed lines no test executes. Coverage never fails the job. |
| `image` | Runs after the three gates. Builds the image with BuildKit layer caching, then runs `scripts/smoke_test.sh` against it, so the Dockerfile is exercised on every PR. **On pushes to `main` and tags only**, it pushes to GHCR, tagged with the commit SHA, the branch, and the semver version for `v*` tags. |

There is deliberately no deploy job: there is no target environment yet. Adding one means
a job gated on a protected GitHub environment that deploys the SHA-tagged image.
`.github/dependabot.yml` opens weekly update PRs for uv dependencies, GitHub Actions and
the Docker base images, and these go through the same gates.

Recommended repository settings once the team grows: branch protection on `main`
requiring the four jobs, at least one review, and a linear history.

## Known limitations

- **Half-duplex clients with incompressible uploads** larger than the output buffer fail
  (after the idle timeout). This is inherent to streaming with bounded memory over
  HTTP/1.1. The alternative, spooling to disk, is ruled out.
- **Per-request throughput is bounded by python-multipart's pure-Python boundary
  scanning** and by DEFLATE. It has not been load-tested yet. Parsing holds the GIL, so many
  simultaneous uploads compete for one core's worth of parsing.
- **HTTP/1.1 only** (uvicorn). HTTP/2 would usually come from a fronting proxy, which
  must not buffer request or response bodies (for example nginx `proxy_request_buffering
  off; proxy_buffering off`), or the streaming and memory guarantees are lost at the
  proxy.
- **Errors after streaming has begun** can only be signalled by truncation. Clients must
  treat a missing terminating chunk or a failing archive as failure.
- **No metrics endpoint.** Operational data is in the structured logs only.
- **Tests depend on Linux `/proc`** for the RSS and disk checks (they skip elsewhere).
- The duplicate check is case-insensitive using Unicode case folding. It doesn't apply
  compatibility normalisation (NFKC), so visually identical names can still coexist.

## With more time

- **Prometheus metrics** (in-flight requests, bytes in/out, outcomes by code, buffer-full
  time) and OpenTelemetry tracing, rather than only parsing logs.
- **A faster multipart parser** (a C-backed scanner, or doing the boundary search with
  `bytes.find` over larger windows) to raise per-request throughput.
- **Load and soak tests** (for example k6 or locust) to validate the memory formula at
  `MAX_CONCURRENT_REQUESTS` and tune the defaults for real traffic.
- **Fuzzing** the processor with Hypothesis (arbitrary chunkings, malformed bodies) and
  property tests asserting that every successful archive round-trips.
- **Image hardening:** digest-pinned bases, a distroless or Chainguard runtime, SBOM and
  provenance attestations, and a vulnerability scan (Trivy or Grype) in CI.
- **A deploy pipeline** with an environment, health-gated rollout and automatic rollback.
- **An optional extended-timestamp extra field**, so tools show the exact UTC time.
