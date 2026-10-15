/* Shared by _macos_native.c and its native test driver; no Python dependency. */
#ifndef CX_MACOS_XPC_H
#define CX_MACOS_XPC_H
#include <dispatch/dispatch.h>
#include <mach/mach_time.h>
#include <stdint.h>
#include <stdlib.h>
#include <time.h>
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

/* MAC01 one-message gate (Hale 16 Oct; design3 sec. 1, strict M2-5 per kai).
 * The connection is NOT suspended: the handler does no work. It holds the FIRST
 * message and returns at once, so any later message is dispatched promptly and,
 * BEFORE the close, POISONS the gate: the connection is cancelled and the whole
 * request fails closed. Order: settle S -> close (the verdict instant, on the
 * peer's serial queue) -> publish (caller) -> the one reply -> cancel. Events
 * after the close are recorded as `late` and never processed. Claimed by
 * construction: every event that reaches the handler before the close poisons.
 * NOT claimed: a message sent before the close that libxpc has not yet
 * dispatched; only strict M2-5c on the test MacBook can show that. */

/* Settle window before the close: a MITIGATION only (Apple documents no dispatch
 * deadline). No pass claim until strict M2-5c runs on the dedicated test MacBook. */
#define CX_GATE_SETTLE_NS 50000000LL

typedef struct {
    xpc_connection_t peer;
    dispatch_queue_t queue;
    dispatch_semaphore_t arrived;
    xpc_object_t first;
    uint64_t closed_at;  /* mach_absolute_time of the close (the verdict instant) */
    int poisoned;   /* second message, non-dictionary event or error before the close */
    int closed;     /* verdict taken (or connection cancelled) */
    int late;       /* an event arrived after the close: never processed */
    int replied;
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
        if (g->closed && !g->poisoned) {  /* after the close: recorded, never processed; our own cancel ignored */
            if (xpc_get_type(event) == XPC_TYPE_DICTIONARY) g->late = 1;
            return;
        }
        if (g->poisoned) return;  /* our own cancel, or events after a poison */
        if (xpc_get_type(event) == XPC_TYPE_DICTIONARY && !g->first) {
            g->first = xpc_retain(event);
            dispatch_semaphore_signal(g->arrived);
            return;
        }
        cx_gate_poison(g);  /* any second message or error before the close */
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

/* Settle S, then the verdict instant on the peer's serial queue: returns 0 if the
 * gate is clean and now closed (the caller may publish), -1 if the request failed
 * closed (poisoned, no first message, or already closed). */
static inline int cx_gate_close(cx_gate_t *g) {
    struct timespec settle = {0, CX_GATE_SETTLE_NS};
    nanosleep(&settle, NULL);
    __block int rc = -1;
    dispatch_sync(g->queue, ^{
        if (g->poisoned || g->closed || !g->first) return;
        g->closed = 1;
        g->closed_at = mach_absolute_time();
        rc = 0;
    });
    return rc;
}

/* After the caller published the result: the one reply, then cancel. Returns 0
 * if replied, -1 if the gate was not cleanly closed or already replied. */
static inline int cx_gate_reply(cx_gate_t *g, const void *data, size_t size) {
    __block int rc = -1;
    dispatch_sync(g->queue, ^{
        if (g->poisoned || !g->closed || g->replied || !g->first) return;
        xpc_object_t reply = xpc_dictionary_create_reply(g->first);
        if (!reply) return;
        xpc_dictionary_set_data(reply, "reply", data, size);
        xpc_connection_send_message(g->peer, reply);
        g->replied = 1;
        rc = 0;
    });
    if (rc == 0) {
        /* cancel only after the reply has been handed to libxpc */
        xpc_connection_send_barrier(g->peer, ^{ xpc_connection_cancel(g->peer); });
    }
    return rc;
}
#endif
