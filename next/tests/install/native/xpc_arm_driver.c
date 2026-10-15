/* Test-only driver (never shipped): an anonymous in-process XPC listener whose
 * peer connection is armed by the product's _macos_xpc.h with argv[1] as the
 * requirement; the same process then acts as the client through an endpoint.
 * No launchd job, Mach service registration or privilege is involved.
 *   argv[2] absent : cx_arm_peer only -> ARMED|ARM_REFUSED then REPLY|NO_REPLY
 *   gate-single    : one message, V replies through the gate
 *   gate-queued    : client queues a SECOND message right after the first
 *   gate-late      : client sends a second message only after the reply
 *   gate-publish   : client sends a second message after the close, before the reply
 * Gate lines: GATE_REFUSED | CLOSED <settle_ms> PUBLISHED GATE_REPLIED, then the client's view.
 */
#include <dispatch/dispatch.h>
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <unistd.h>
#include "_macos_xpc.h"

static xpc_connection_t cx_client(xpc_connection_t listener) {
    xpc_connection_t client = xpc_connection_create_from_endpoint(xpc_endpoint_create(listener));
    xpc_connection_set_event_handler(client, ^(xpc_object_t e) { (void)e; });
    xpc_connection_resume(client);
    return client;
}

static xpc_object_t cx_message(const char *text) {
    xpc_object_t m = xpc_dictionary_create(NULL, NULL, 0);
    xpc_dictionary_set_string(m, "request", text);
    return m;
}

static int arm_only(const char *requirement) {
    dispatch_queue_t q = dispatch_queue_create("cx.test", DISPATCH_QUEUE_SERIAL);
    xpc_connection_t listener = xpc_connection_create(NULL, q);
    xpc_connection_set_event_handler(listener, ^(xpc_object_t peer) {
        if (xpc_get_type(peer) != XPC_TYPE_CONNECTION) return;
        int rc = cx_arm_peer((xpc_connection_t)peer, requirement, ^(xpc_object_t m) {
            if (xpc_get_type(m) != XPC_TYPE_DICTIONARY) return;
            xpc_object_t r = xpc_dictionary_create_reply(m);
            if (r) { xpc_dictionary_set_string(r, "reply", "ok"); xpc_connection_send_message((xpc_connection_t)peer, r); }
        });
        printf("%s\n", rc == 0 ? "ARMED" : "ARM_REFUSED");
        fflush(stdout);
    });
    xpc_connection_resume(listener);
    xpc_object_t r = xpc_connection_send_message_with_reply_sync(cx_client(listener), cx_message("ping"));
    const char *got = xpc_get_type(r) == XPC_TYPE_DICTIONARY ? xpc_dictionary_get_string(r, "reply") : NULL;
    dispatch_sync(q, ^{});  /* the listener's ARMED/ARM_REFUSED line is printed first */
    printf("%s\n", got && !strcmp(got, "ok") ? "REPLY" : "NO_REPLY");
    return 0;
}

static int gate(const char *requirement, const char *mode) {
    __block cx_gate_t *g = NULL;
    dispatch_semaphore_t armed = dispatch_semaphore_create(0);
    xpc_connection_t listener = xpc_connection_create(NULL, dispatch_queue_create("cx.listener", DISPATCH_QUEUE_SERIAL));
    xpc_connection_set_event_handler(listener, ^(xpc_object_t peer) {
        if (xpc_get_type(peer) != XPC_TYPE_CONNECTION || g) return;
        g = cx_gate_arm((xpc_connection_t)peer, requirement);
        dispatch_semaphore_signal(armed);
    });
    xpc_connection_resume(listener);
    xpc_connection_t client = cx_client(listener);
    dispatch_semaphore_t answered = dispatch_semaphore_create(0);
    __block const char *answer = "NO_ANSWER";
    xpc_connection_send_message_with_reply(client, cx_message("first"), dispatch_get_global_queue(0, 0), ^(xpc_object_t r) {
        size_t n = 0;
        answer = xpc_get_type(r) == XPC_TYPE_DICTIONARY && xpc_dictionary_get_data(r, "reply", &n) ? "CLIENT_GOT_REPLY"
                                                                                                   : "CLIENT_GOT_ERROR";
        dispatch_semaphore_signal(answered);
    });
    if (!strcmp(mode, "gate-queued")) xpc_connection_send_message(client, cx_message("second"));
    dispatch_semaphore_wait(armed, dispatch_time(DISPATCH_TIME_NOW, 2000000000LL));
    xpc_object_t first = g ? cx_gate_first(g, 2000000000LL) : NULL;
    if (!first) {  /* poisoned before V took the first message: refused even earlier */
        __block int poisoned = 0;
        if (g) dispatch_sync(g->queue, ^{ poisoned = g->poisoned; });
        printf("%s\n", poisoned ? "GATE_REFUSED" : "GATE_NO_MESSAGE");
        if (poisoned) {
            dispatch_semaphore_wait(answered, dispatch_time(DISPATCH_TIME_NOW, 2000000000LL));
            printf("%s\n", answer);
        }
        return 0;
    }
    usleep(200000);  /* V's owner work on the first request */
    struct timespec t0, t1;
    clock_gettime(CLOCK_MONOTONIC, &t0);
    if (cx_gate_close(g) != 0) {  /* verdict instant: poisoned before it -> nothing published */
        printf("GATE_REFUSED\n");
        dispatch_semaphore_wait(answered, dispatch_time(DISPATCH_TIME_NOW, 2000000000LL));
        printf("%s\n", answer);
        return 0;
    }
    clock_gettime(CLOCK_MONOTONIC, &t1);
    printf("CLOSED %lld\n", ((t1.tv_sec - t0.tv_sec) * 1000000000LL + (t1.tv_nsec - t0.tv_nsec)) / 1000000);
    if (!strcmp(mode, "gate-publish")) {
        xpc_connection_send_message(client, cx_message("after-close"));
        usleep(200000);  /* the message is dispatched while V publishes */
    }
    printf("PUBLISHED\n");  /* the caller's publication, strictly after the close */
    int rc = cx_gate_reply(g, "ok", 2);
    printf("%s\n", rc == 0 ? "GATE_REPLIED" : "GATE_REFUSED");
    dispatch_semaphore_wait(answered, dispatch_time(DISPATCH_TIME_NOW, 2000000000LL));
    printf("%s\n", answer);
    if (!strcmp(mode, "gate-publish")) {
        __block int late = 0;
        dispatch_sync(g->queue, ^{ late = g->late; });
        printf("%s\n", late ? "LATE_SEEN_NOT_PROCESSED" : "LATE_UNSEEN");
    }
    if (!strcmp(mode, "gate-late")) {
        xpc_connection_send_message(client, cx_message("late"));
        usleep(200000);
        __block int poisoned = 0;
        dispatch_sync(g->queue, ^{ poisoned = g->poisoned; });
        printf("%s\n", poisoned ? "LATE_PROCESSED" : "LATE_NOT_PROCESSED");
    }
    return 0;
}

int main(int argc, char **argv) {
    if (argc == 2) return arm_only(argv[1]);
    if (argc == 3) return gate(argv[1], argv[2]);
    return 64;
}
