/* Test-only driver (never shipped): exercises the product's cx_verify_listener on
 * a real fd 3. argv[1] = socket path, argv[2] = case. Prints VERIFIED or REFUSED. */
#define _GNU_SOURCE
#include <stdio.h>
#include <string.h>
#include <unistd.h>
#include "_linux_listener.h"

int main(int argc, char **argv) {
    if (argc != 3) return 64;
    const char *path = argv[1], *mode = argv[2];
    int s = socket(AF_UNIX, mode[0] == 'd' ? SOCK_DGRAM : SOCK_STREAM, 0);
    struct sockaddr_un a;
    struct stat st;
    memset(&a, 0, sizeof a);
    a.sun_family = AF_UNIX;
    strncpy(a.sun_path, path, sizeof a.sun_path - 1);
    unlink(path);
    if (s < 0 || bind(s, (struct sockaddr *)&a, sizeof a)) return 65;
    if (strcmp(mode, "unlistened") && mode[0] != 'd' && listen(s, 1)) return 66;
    if (dup2(s, 3) != 3 || lstat(path, &st)) return 67;
    if (!strcmp(mode, "replaced")) {  /* same path, different inode */
        unlink(path);
        int t = socket(AF_UNIX, SOCK_STREAM, 0);
        if (t < 0 || bind(t, (struct sockaddr *)&a, sizeof a)) return 68;
    }
    const char *expect = !strcmp(mode, "wrongpath") ? "/run/not-the-endpoint.sock" : path;
    printf("%s\n", cx_verify_listener(3, expect, st.st_dev, st.st_ino) == 0 ? "VERIFIED" : "REFUSED");
    return 0;
}
