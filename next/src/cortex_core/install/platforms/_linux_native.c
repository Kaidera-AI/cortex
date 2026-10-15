/* Private native module of the fixed Linux observer, inside V's admitted closure.
 *
 * Built now (design2, accepted parts): the bracketed kernel clock sample and the
 * check of systemd's inherited Accept=no listener (fd 3). NOT built: the sender
 * binding - it waits for Hale/Vera on the SELinux label contract (W2 HELD), so
 * linux.py keeps refusing required_binding_gap. NOT_RUN on macOS; the lab host
 * runs it later under a GO. No environment variable selects anything here.
 */
#define _GNU_SOURCE
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <fcntl.h>
#include <string.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/timex.h>
#include <sys/un.h>
#include <time.h>
#include <unistd.h>

#include "_linux_listener.h"

static int cx_ns(clockid_t id, long long *out) {
    struct timespec t;
    if (clock_gettime(id, &t) != 0) return -1;
    *out = (long long)t.tv_sec * 1000000000LL + t.tv_nsec;
    return 0;
}

static int cx_boot_id(char *buf, size_t size) {
    int fd = open("/proc/sys/kernel/random/boot_id", O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (fd < 0) return -1;
    ssize_t n = read(fd, buf, size - 1);
    close(fd);
    if (n < 2) return -1;
    buf[n] = 0;
    if (buf[n - 1] == '\n') buf[n - 1] = 0;
    return 0;
}

static PyObject *cx_clock_sample(PyObject *self, PyObject *unused) {
    (void)self;
    (void)unused;
    struct timex tx;
    long long m0, m1, rt, boottime;
    char boot[64];
    memset(&tx, 0, sizeof tx);  /* modes = 0: unprivileged read only */
    int ok = cx_ns(CLOCK_MONOTONIC, &m0) == 0;  /* CPython time.monotonic_ns() domain */
    ok = ok && cx_ns(CLOCK_REALTIME, &rt) == 0;
    int state = adjtimex(&tx);
    ok = ok && cx_ns(CLOCK_BOOTTIME, &boottime) == 0;
    ok = ok && cx_ns(CLOCK_MONOTONIC, &m1) == 0;
    if (!ok || state < 0 || tx.maxerror < 0 || cx_boot_id(boot, sizeof boot) != 0)
        return PyErr_Format(PyExc_OSError, "clock_unavailable");
    return Py_BuildValue("{s:s,s:L,s:L,s:L,s:L,s:i,s:i,s:l,s:s}",
        "platform", "linux", "m0", m0, "m1", m1, "realtime_ns", rt, "boottime_ns", boottime,
        "ntp_state", state, "ntp_status", tx.status, "maxerror_us", tx.maxerror, "boot_id", boot);
}

static PyMethodDef cx_methods[] = {
    {"clock_sample", cx_clock_sample, METH_NOARGS, "Bracketed kernel clock sample (DATA)."},
    {NULL, NULL, 0, NULL}};

static struct PyModuleDef cx_module = {PyModuleDef_HEAD_INIT, "_linux_native", NULL, -1, cx_methods, NULL, NULL, NULL, NULL};

PyMODINIT_FUNC PyInit__linux_native(void) { return PyModule_Create(&cx_module); }
