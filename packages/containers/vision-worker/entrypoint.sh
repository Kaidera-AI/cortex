#!/usr/bin/env bash
# Supervise the colocated Ollama daemon and the FastAPI adapter as one lifecycle.

set -euo pipefail

OLLAMA_PID=""
UVICORN_PID=""

# shellcheck disable=SC2329  # invoked by EXIT trap
cleanup() {
    trap - EXIT
    for pid in "${UVICORN_PID}" "${OLLAMA_PID}"; do
        if [[ -n "${pid}" ]] && kill -0 "${pid}" 2>/dev/null; then
            kill -TERM "${pid}" 2>/dev/null || true
        fi
    done
    for pid in "${UVICORN_PID}" "${OLLAMA_PID}"; do
        if [[ -n "${pid}" ]]; then
            wait "${pid}" 2>/dev/null || true
        fi
    done
}
trap cleanup EXIT
trap 'exit 0' TERM INT

ollama serve &
OLLAMA_PID=$!

echo "[entrypoint] waiting for ollama..."
OLLAMA_READY=0
for _ in {1..30}; do
    if ! kill -0 "${OLLAMA_PID}" 2>/dev/null; then
        set +e
        wait "${OLLAMA_PID}"
        OLLAMA_STATUS=$?
        set -e
        OLLAMA_PID=""
        echo "[entrypoint] ERROR: ollama exited during startup with status ${OLLAMA_STATUS}" >&2
        exit 1
    fi
    if curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
        OLLAMA_READY=1
        echo "[entrypoint] ollama ready"
        break
    fi
    sleep 1
done
if [[ "${OLLAMA_READY}" -ne 1 ]]; then
    echo "[entrypoint] ERROR: ollama did not become ready within 30 seconds" >&2
    exit 1
fi

uvicorn worker:app --host 0.0.0.0 --port 9002 &
UVICORN_PID=$!

# Bash 3-compatible supervision: exit the container when either colocated
# process dies. In particular, a dead Ollama daemon must not leave the FastAPI
# adapter serving a superficially live container forever.
while true; do
    if ! kill -0 "${OLLAMA_PID}" 2>/dev/null; then
        set +e
        wait "${OLLAMA_PID}"
        OLLAMA_STATUS=$?
        set -e
        OLLAMA_PID=""
        echo "[entrypoint] ERROR: ollama exited with status ${OLLAMA_STATUS}" >&2
        exit 1
    fi
    if ! kill -0 "${UVICORN_PID}" 2>/dev/null; then
        set +e
        wait "${UVICORN_PID}"
        STATUS=$?
        set -e
        UVICORN_PID=""
        exit "${STATUS}"
    fi
    sleep 1
done
