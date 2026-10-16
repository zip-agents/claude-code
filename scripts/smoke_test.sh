#!/usr/bin/env bash
# Smoke-test a built image the way production runs it: read-only root filesystem,
# no capabilities, non-root. Requires: docker (or ENGINE=podman), curl, unzip.
#
#   ./scripts/smoke_test.sh [image]        (default image: zip-service:local)
set -euo pipefail

IMAGE="${1:-zip-service:local}"
ENGINE="${ENGINE:-docker}"
HOST_PORT="${HOST_PORT:-18080}"
NAME="zip-smoke-$$"
NAME_PORT="zip-smoke-port-$$"
NAME_OFFLINE="zip-smoke-offline-$$"
WORK="$(mktemp -d)"

cleanup() {
  "$ENGINE" rm -f "$NAME" "$NAME_PORT" "$NAME_OFFLINE" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

fail() { echo "FAIL: $*" >&2; "$ENGINE" logs "$NAME" >&2 2>/dev/null || true; exit 1; }
pass() { echo "ok - $*"; }

hardened=(--read-only --cap-drop ALL --security-opt no-new-privileges)

wait_live() {
  local url="$1"
  for _ in $(seq 1 50); do
    curl -fsS "$url/health/live" >/dev/null 2>&1 && return 0
    sleep 0.2
  done
  return 1
}

echo "# image: $IMAGE"
"$ENGINE" run -d --name "$NAME" "${hardened[@]}" -p "127.0.0.1:${HOST_PORT}:8000" "$IMAGE" >/dev/null
BASE="http://127.0.0.1:${HOST_PORT}"
wait_live "$BASE" || fail "service did not become live"
pass "starts with a read-only root filesystem and no capabilities"

uid="$("$ENGINE" exec "$NAME" id -u)"
[ "$uid" != "0" ] || fail "container runs as root"
pass "runs as non-root user (uid $uid)"

[ "$(curl -fsS "$BASE/health/live")" = '{"status":"ok"}' ] || fail "/health/live body"
[ "$(curl -fsS "$BASE/health/ready")" = '{"status":"ready"}' ] || fail "/health/ready body"
pass "health endpoints"

printf 'hello\n' > "$WORK/a.txt"
head -c 200000 /dev/urandom > "$WORK/photo.jpg"
curl -sS -o "$WORK/out.zip" -D "$WORK/headers.txt" \
  -F 'filename=quarterly report' \
  -F "files=@$WORK/a.txt" \
  -F "files=@$WORK/a.txt" \
  -F "files=@$WORK/photo.jpg" \
  "$BASE/api/v1/zip"
grep -qi '^HTTP/1.1 200' "$WORK/headers.txt" || fail "zip request did not return 200"
grep -qi "^content-disposition: attachment; filename=\"quarterly report.zip\"; filename\*=UTF-8''quarterly%20report.zip" \
  "$WORK/headers.txt" || fail "Content-Disposition header"
grep -qi '^content-type: application/zip' "$WORK/headers.txt" || fail "Content-Type header"
grep -qi '^transfer-encoding: chunked' "$WORK/headers.txt" || fail "not chunked"
grep -qi '^x-request-id: ' "$WORK/headers.txt" || fail "missing X-Request-ID"
unzip -t "$WORK/out.zip" >/dev/null || fail "unzip -t rejected the archive"
listing="$(unzip -Z1 "$WORK/out.zip" | tr '\n' '|')"
[ "$listing" = "a.txt|a (1).txt|photo.jpg|" ] || fail "unexpected entries: $listing"
unzip -p "$WORK/out.zip" photo.jpg | cmp -s - "$WORK/photo.jpg" || fail "content mismatch"
pass "contract example produces a valid archive"

status="$(curl -sS -o "$WORK/problem.json" -w '%{http_code}' -H 'Content-Type: application/json' \
  --data '{}' "$BASE/api/v1/zip")"
[ "$status" = "415" ] || fail "expected 415, got $status"
grep -q '"code":"unsupported-media-type"' "$WORK/problem.json" || fail "problem body"
pass "errors are problem+json"

"$ENGINE" logs "$NAME" 2>/dev/null | python3 -c '
import json, sys
lines = [l for l in sys.stdin.read().splitlines() if l.strip()]
assert lines, "no log output"
for line in lines:
    json.loads(line)
' || fail "stdout is not JSON lines"
pass "logs are JSON lines"

if [ "$ENGINE" = "docker" ]; then
  for _ in $(seq 1 30); do
    health="$("$ENGINE" inspect -f '{{.State.Health.Status}}' "$NAME")"
    [ "$health" = "healthy" ] && break
    sleep 1
  done
  [ "$health" = "healthy" ] || fail "HEALTHCHECK status is $health"
  pass "HEALTHCHECK reports healthy"
fi

"$ENGINE" stop -t 30 "$NAME" >/dev/null
code="$("$ENGINE" inspect -f '{{.State.ExitCode}}' "$NAME")"
[ "$code" = "0" ] || fail "exit code after SIGTERM was $code"
"$ENGINE" logs "$NAME" 2>/dev/null | grep -q 'Shutdown started' || fail "no graceful shutdown log"
pass "SIGTERM shuts down gracefully with exit code 0"

"$ENGINE" run -d --name "$NAME_PORT" "${hardened[@]}" -e PORT=9123 \
  -p "127.0.0.1:$((HOST_PORT + 1)):9123" "$IMAGE" >/dev/null
wait_live "http://127.0.0.1:$((HOST_PORT + 1))" || fail "service did not honour PORT"
pass "listens on \$PORT"

"$ENGINE" run -d --name "$NAME_OFFLINE" "${hardened[@]}" --network none "$IMAGE" >/dev/null
sleep 2
"$ENGINE" exec "$NAME_OFFLINE" python -c \
  "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=2)" \
  || fail "service does not work without network access"
pass "runs without network access"

if "$ENGINE" run --rm -e MAX_FILES=0 "$IMAGE" >/dev/null 2>"$WORK/config.err"; then
  fail "invalid configuration did not exit non-zero"
fi
grep -q MAX_FILES "$WORK/config.err" || fail "invalid configuration message"
pass "invalid configuration exits non-zero with a clear message"

echo "# all smoke tests passed"
