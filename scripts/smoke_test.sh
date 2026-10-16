#!/usr/bin/env bash
# Smoke-test a built image the way production runs it: read-only root filesystem,
# no capabilities, non-root. Only Docker (or ENGINE=podman) is needed on the host:
# HTTP and archive checks run in PROBE_IMAGE (the dev image, built by `make dev-image`),
# attached to the service container's network namespace.
#
#   ./scripts/smoke_test.sh [image]        (default image: zip-service:local)
set -euo pipefail

IMAGE="${1:-zip-service:local}"
ENGINE="${ENGINE:-docker}"
PROBE_IMAGE="${PROBE_IMAGE:-zip-service-dev}"
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

# probe CONTAINER SCRIPT: run a bash script next to CONTAINER (shares its localhost).
probe() {
  local target="$1"
  shift
  "$ENGINE" run --rm --network "container:$target" -v "$WORK:/work" "$PROBE_IMAGE" \
    bash -euo pipefail -c "$1"
}

wait_live() {
  probe "$1" "for _ in \$(seq 1 50); do
      curl -fsS http://127.0.0.1:$2/health/live >/dev/null 2>&1 && exit 0
      sleep 0.2
    done
    exit 1"
}

echo "# image: $IMAGE (probe: $PROBE_IMAGE)"
"$ENGINE" run -d --name "$NAME" "${hardened[@]}" "$IMAGE" >/dev/null
wait_live "$NAME" 8000 || fail "service did not become live"
pass "starts with a read-only root filesystem and no capabilities"

uid="$("$ENGINE" exec "$NAME" id -u)"
[ "$uid" != "0" ] || fail "container runs as root"
pass "runs as non-root user (uid $uid)"

probe "$NAME" '
  [ "$(curl -fsS http://127.0.0.1:8000/health/live)" = "{\"status\":\"ok\"}" ]
  [ "$(curl -fsS http://127.0.0.1:8000/health/ready)" = "{\"status\":\"ready\"}" ]
' || fail "health endpoints"
pass "health endpoints"

probe "$NAME" '
  cd /work
  printf "hello\n" > a.txt
  head -c 200000 /dev/urandom > photo.jpg
  curl -sS -o out.zip -D headers.txt \
    -F "filename=quarterly report" -F "files=@a.txt" -F "files=@a.txt" -F "files=@photo.jpg" \
    http://127.0.0.1:8000/api/v1/zip
  grep -qi "^HTTP/1.1 200" headers.txt || { echo "status"; exit 1; }
  grep -qi "^content-disposition: attachment; filename=\"quarterly report.zip\"; filename\*=UTF-8'"''"'quarterly%20report.zip" headers.txt \
    || { echo "Content-Disposition"; exit 1; }
  grep -qi "^content-type: application/zip" headers.txt || { echo "Content-Type"; exit 1; }
  grep -qi "^transfer-encoding: chunked" headers.txt || { echo "chunked"; exit 1; }
  grep -qi "^x-request-id: " headers.txt || { echo "X-Request-ID"; exit 1; }
  unzip -t out.zip >/dev/null
  7z t out.zip >/dev/null
  [ "$(unzip -Z1 out.zip | tr "\n" "|")" = "a.txt|a (1).txt|photo.jpg|" ] || { echo "entries"; exit 1; }
  unzip -p out.zip photo.jpg | cmp -s - photo.jpg
' || fail "contract example"
pass "contract example produces a valid archive (unzip -t, 7z t, byte-for-byte)"

probe "$NAME" '
  status="$(curl -sS -o /work/problem.json -w "%{http_code}" -H "Content-Type: application/json" \
    --data "{}" http://127.0.0.1:8000/api/v1/zip)"
  [ "$status" = "415" ]
  grep -q "\"code\":\"unsupported-media-type\"" /work/problem.json
' || fail "problem responses"
pass "errors are problem+json"

"$ENGINE" logs "$NAME" 2>/dev/null | "$ENGINE" run --rm -i "$PROBE_IMAGE" python3 -c '
import json, sys
lines = [l for l in sys.stdin.read().splitlines() if l.strip()]
assert lines, "no log output"
for line in lines:
    json.loads(line)
' || fail "stdout is not JSON lines"
pass "logs are JSON lines"

if [ "$ENGINE" = "docker" ]; then
  health=unknown
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

"$ENGINE" run -d --name "$NAME_PORT" "${hardened[@]}" -e PORT=9123 "$IMAGE" >/dev/null
wait_live "$NAME_PORT" 9123 || fail "service did not honour PORT"
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
