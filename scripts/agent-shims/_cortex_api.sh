#!/usr/bin/env bash
# Cortex release-owned member API bridge. No bearer enters this shell.

CORTEX_SHIM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Legacy callers use this only in error text. Do not echo unvalidated URL env.
CORTEX_API="selected Cortex v2 origin"

cortex_api_call() {
    if [ "$#" -lt 2 ] || [ "$#" -gt 4 ] || [ -n "${3:-}" ] \
       || [ -n "${CORTEX_API_PAYLOAD_FILE:-}" ] || [ "${1:-}" != GET ] \
       || [ "${2:-}" != /projects ] || [ -n "${CORTEX_CTO_OVERRIDE:-}" ] \
       || [ "${CORTEX_API_WITH_ADMIN:-0}" != 0 ]; then
        printf '%s\n' 'ERROR: unqualified member facade request' >&2
        return 2
    fi
    if [ -z "${CORTEX_CONNECTION_PROFILE:-}" ]; then
        printf '%s\n' 'ERROR: select CORTEX_CONNECTION_PROFILE (non-secret v2 member profile)' >&2
        return 2
    fi
    if [ -n "${4:-}" ]; then
        "${CORTEX_SHIM_DIR}/cortex-agent" --config "${CORTEX_CONNECTION_PROFILE}" api GET /projects --agent-name "$4" </dev/null
    else
        "${CORTEX_SHIM_DIR}/cortex-agent" --config "${CORTEX_CONNECTION_PROFILE}" api GET /projects </dev/null
    fi
}

cortex_api_json() { cortex_api_call "$@"; }
cortex_api_call_json() { cortex_api_call "$@"; }
cortex_api_call_admin() {
    printf '%s\n' 'ERROR: privileged requests require the owner workflow' >&2
    return 2
}
