/* Shared by _macos_native.c and its native test driver; no Python dependency. */
#ifndef CX_MACOS_XPC_H
#define CX_MACOS_XPC_H
#include <xpc/xpc.h>

/* Arm ONE peer connection: the fixed requirement is set exactly once and its
 * return code checked BEFORE resume (macOS 12 API; deployment floor 13). Any
 * failure cancels the peer: no PID-only or unchecked fallback. Returns 0 if armed. */
static inline int cx_arm_peer(xpc_connection_t peer, const char *requirement, xpc_handler_t handler) {
    if (!peer || !requirement || !*requirement || !handler) return -1;
    if (xpc_connection_set_peer_code_signing_requirement(peer, requirement) != 0) {
        xpc_connection_cancel(peer);
        return -1;
    }
    xpc_connection_set_event_handler(peer, handler);
    xpc_connection_resume(peer);
    return 0;
}

#endif
