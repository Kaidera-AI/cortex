/* The ONE fixed admitted C client (design Q4): the only program V's requirement /
 * SELinux domain admits as caller. It moves bounded opaque request bytes from
 * stdin to V and the reply to stdout. It judges nothing: V's common owner still
 * parses the request and checks consent, policy and scope. Its identity is this
 * client only, never its parent console or stdin producer.
 *
 * Build inputs are fixed per release (part of the signed closure), not runtime
 * configuration: macOS -DCX_SERVICE="label" -DCX_OWNER_REQUIREMENT="requirement";
 * POSIX/Linux -DCX_SOCKET="/absolute/path". Exit 0 = reply printed, 2 = refused.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define CX_MAX_REQUEST 1048576  /* same bound as install/__main__ request_reader */
#define CX_MAX_REPLY 1048576

static unsigned char cx_buf[CX_MAX_REQUEST + 1];

static long cx_read_request(void) {
    size_t got = 0;
    ssize_t n;
    while ((n = read(0, cx_buf + got, sizeof cx_buf - got)) > 0) got += (size_t)n;
    if (n < 0 || got == 0 || got > CX_MAX_REQUEST) return -1;  /* empty or oversize refuses */
    return (long)got;
}

#if defined(__APPLE__) && !defined(CX_POSIX)
#include <xpc/xpc.h>
#ifndef CX_SERVICE
#error "CX_SERVICE (fixed launchd label) is a required build input"
#endif
#ifndef CX_OWNER_REQUIREMENT
#error "CX_OWNER_REQUIREMENT (fixed V code requirement) is a required build input"
#endif

int main(void) {
    long len = cx_read_request();
    if (len < 0) return 2;
    xpc_connection_t c = xpc_connection_create_mach_service(CX_SERVICE, NULL, XPC_CONNECTION_MACH_SERVICE_PRIVILEGED);
    /* Mutual: the client also refuses any service that is not the admitted V. */
    if (xpc_connection_set_peer_code_signing_requirement(c, CX_OWNER_REQUIREMENT) != 0) return 2;
    xpc_connection_set_event_handler(c, ^(xpc_object_t e) { (void)e; });
    xpc_connection_resume(c);
    xpc_object_t m = xpc_dictionary_create(NULL, NULL, 0);
    xpc_dictionary_set_data(m, "request", cx_buf, (size_t)len);
    xpc_object_t r = xpc_connection_send_message_with_reply_sync(c, m);
    size_t n = 0;
    const void *reply = xpc_get_type(r) == XPC_TYPE_DICTIONARY ? xpc_dictionary_get_data(r, "reply", &n) : NULL;
    if (!reply || n == 0 || n > CX_MAX_REPLY || fwrite(reply, 1, n, stdout) != n) return 2;
    return fflush(stdout) == 0 ? 0 : 2;
}

#else
#include <sys/socket.h>
#include <sys/un.h>
#ifndef CX_SOCKET
#error "CX_SOCKET (fixed endpoint path) is a required build input"
#endif

int main(void) {
    long len = cx_read_request();
    if (len < 0) return 2;
    struct sockaddr_un a;
    memset(&a, 0, sizeof a);
    a.sun_family = AF_UNIX;
    if (strlen(CX_SOCKET) >= sizeof a.sun_path) return 2;
    strcpy(a.sun_path, CX_SOCKET);
    int s = socket(AF_UNIX, SOCK_STREAM, 0);
    if (s < 0 || connect(s, (struct sockaddr *)&a, sizeof a) != 0) return 2;
    for (long off = 0; off < len;) {  /* plain bytes only: never SCM_RIGHTS */
        ssize_t w = write(s, cx_buf + off, (size_t)(len - off));
        if (w <= 0) return 2;
        off += w;
    }
    if (shutdown(s, SHUT_WR) != 0) return 2;
    size_t got = 0;
    ssize_t n;
    while ((n = read(s, cx_buf + got, sizeof cx_buf - got)) > 0) got += (size_t)n;
    if (n < 0 || got == 0 || got > CX_MAX_REPLY || fwrite(cx_buf, 1, got, stdout) != got) return 2;
    return fflush(stdout) == 0 ? 0 : 2;
}
#endif
