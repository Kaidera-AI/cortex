/* Shared by _macos_native.c and its native test driver; no Python dependency. */
#ifndef CX_MACOS_XPC_H
#define CX_MACOS_XPC_H
#include <dispatch/dispatch.h>
#include <stdlib.h>
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

/* MAC01 one-message gate (Hale 16 Oct). The connection is NOT suspended:
 * suspension only defers the handler that could see a second message. The
 * handler does no work: it holds the FIRST message and returns at once, so any
 * later message is dispatched promptly and POISONS the gate: the connection is
 * cancelled and the whole request fails closed. The final check and the one
 * reply run together on the connection's own serial queue, so no message
 * dispatched before the reply can be missed. A message that is dispatched only
 * after the reply was sent is never processed (the gate is closed). */
typedef struct {
    xpc_connection_t peer;
    dispatch_queue_t queue;
    dispatch_semaphore_t arrived;
    xpc_object_t first;
    int poisoned;   /* second message, non-dictionary event or error */
    int closed;     /* reply sent or connection cancelled */
} cx_gate_t;

static inline void cx_gate_poison(cx_gate_t *g) {
    g->poisoned = 1;
    if (!g->closed) {
        g->closed = 1;
        xpc_connection_cancel(g->peer);
    }
    dispatch_semaphore_signal(g->arrived);  /* never leave a waiter hanging */
}

/* Returns a gate for this peer, or NULL (peer cancelled) if arming fails. */
static inline cx_gate_t *cx_gate_arm(xpc_connection_t peer, const char *requirement) {
    cx_gate_t *g = calloc(1, sizeof *g);
    if (!g) { xpc_connection_cancel(peer); return NULL; }
    g->peer = peer;
    g->queue = dispatch_queue_create("ai.kaidera.cortex.owner.peer", DISPATCH_QUEUE_SERIAL);
    g->arrived = dispatch_semaphore_create(0);
    xpc_connection_set_target_queue(peer, g->queue);
    int rc = cx_arm_peer(peer, requirement, ^(xpc_object_t event) {
        if (g->closed && !g->poisoned && xpc_get_type(event) != XPC_TYPE_DICTIONARY) return;  /* our own cancel */
        if (xpc_get_type(event) == XPC_TYPE_DICTIONARY && !g->first && !g->poisoned && !g->closed) {
            g->first = xpc_retain(event);
            dispatch_semaphore_signal(g->arrived);
            return;
        }
        cx_gate_poison(g);  /* any second message, late message or error */
    });
    if (rc != 0) { free(g); return NULL; }
    return g;
}

/* Wait for the first message; returns it (owned by the gate) or NULL. */
static inline xpc_object_t cx_gate_first(cx_gate_t *g, int64_t timeout_ns) {
    if (dispatch_semaphore_wait(g->arrived, dispatch_time(DISPATCH_TIME_NOW, timeout_ns)) != 0) return NULL;
    __block xpc_object_t first = NULL;
    dispatch_sync(g->queue, ^{ if (!g->poisoned) first = g->first; });
    return first;
}

/* Atomic final check + the one reply. Returns 0 if replied, -1 if the request
 * failed closed (second message seen, error, or already closed). */
static inline int cx_gate_reply(cx_gate_t *g, const void *data, size_t size) {
    __block int rc = -1;
    dispatch_sync(g->queue, ^{
        if (g->poisoned || g->closed || !g->first) return;
        xpc_object_t reply = xpc_dictionary_create_reply(g->first);
        if (!reply) { cx_gate_poison(g); return; }
        xpc_dictionary_set_data(reply, "reply", data, size);
        xpc_connection_send_message(g->peer, reply);
        g->closed = 1;  /* after this point every event is ignored or poisons nothing new */
        rc = 0;
    });
    return rc;
}
#endif
