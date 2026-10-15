/* Shared by _linux_native.c and its native test driver; no Python dependency. */
#ifndef CX_LINUX_LISTENER_H
#define CX_LINUX_LISTENER_H
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>

/* systemd Accept=no hands V the listening socket as fd 3. Verify it is exactly
 * the admitted pathname stream listener (family, type, listening, path, inode)
 * instead of trusting LISTEN_FDS. V never unlinks or shuts it down. 0 = verified. */
static inline int cx_verify_listener(int fd, const char *path, dev_t dev, ino_t ino) {
    int domain = 0, type = 0, listening = 0;
    socklen_t len = sizeof domain;
    struct sockaddr_un addr;
    struct stat st;
    if (getsockopt(fd, SOL_SOCKET, SO_DOMAIN, &domain, &len) || domain != AF_UNIX) return -1;
    len = sizeof type;
    if (getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &len) || type != SOCK_STREAM) return -1;
    len = sizeof listening;
    if (getsockopt(fd, SOL_SOCKET, SO_ACCEPTCONN, &listening, &len) || listening != 1) return -1;
    len = sizeof addr;
    memset(&addr, 0, sizeof addr);
    if (getsockname(fd, (struct sockaddr *)&addr, &len) || addr.sun_family != AF_UNIX
            || strncmp(addr.sun_path, path, sizeof addr.sun_path) != 0) return -1;
    if (lstat(path, &st) || !S_ISSOCK(st.st_mode) || st.st_dev != dev || st.st_ino != ino) return -1;
    return 0;
}

#endif
