/* Test-only driver (never shipped): an anonymous in-process XPC listener whose
 * one peer connection is armed by the product's cx_arm_peer with argv[1] as the
 * requirement; the same process then sends one message through an endpoint.
 * Prints ARMED/ARM_REFUSED for the peer and REPLY/NO_REPLY for the client. No
 * launchd job, Mach service registration or privilege is involved. */
#include <dispatch/dispatch.h>
#include <stdio.h>
#include <string.h>
#include "_macos_xpc.h"

int main(int argc, char **argv) {
    if (argc != 2) return 64;
    const char *requirement = argv[1];
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
    xpc_connection_t client = xpc_connection_create_from_endpoint(xpc_endpoint_create(listener));
    xpc_connection_set_event_handler(client, ^(xpc_object_t e) { (void)e; });
    xpc_connection_resume(client);
    xpc_object_t m = xpc_dictionary_create(NULL, NULL, 0);
    xpc_dictionary_set_string(m, "request", "ping");
    xpc_object_t r = xpc_connection_send_message_with_reply_sync(client, m);
    const char *got = xpc_get_type(r) == XPC_TYPE_DICTIONARY ? xpc_dictionary_get_string(r, "reply") : NULL;
    dispatch_sync(q, ^{});  /* the listener's ARMED/ARM_REFUSED line is printed first */
    printf("%s\n", got && !strcmp(got, "ok") ? "REPLY" : "NO_REPLY");
    return 0;
}
