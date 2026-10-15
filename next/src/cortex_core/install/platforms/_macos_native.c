/* Private native module of the fixed macOS observer, inside V's admitted closure.
 *
 * Built now (design2, accepted parts): the bracketed kernel clock sample and the
 * peer-requirement arming of one XPC peer connection. NOT built: the message
 * sender binding (MAC01) - it waits for receipt M2, so macos.py keeps refusing
 * required_binding_gap and nothing here is reachable from the owner seam.
 * No environment, file, argv or caller input selects anything in this module.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <mach/mach_time.h>
#include <string.h>
#include <sys/sysctl.h>
#include <sys/timex.h>
#include <time.h>
#include "_macos_xpc.h"

/* CPython 3.12 time.monotonic_ns(): mach_absolute_time scaled by timebase,
 * using the same overflow-safe split as _PyTime_MulDiv. */
static mach_timebase_info_data_t cx_timebase;

static int64_t cx_scale(uint64_t ticks) {
    uint64_t q = ticks / cx_timebase.denom, r = ticks % cx_timebase.denom;
    return (int64_t)(q * cx_timebase.numer + r * cx_timebase.numer / cx_timebase.denom);
}

static PyObject *cx_clock_sample(PyObject *self, PyObject *unused) {
    (void)self;
    (void)unused;
    struct timespec rt;
    struct ntptimeval ntv;
    struct timex tx;
    char boot[64] = {0};
    size_t boot_len = sizeof boot - 1;
    memset(&tx, 0, sizeof tx);  /* modes = 0: read only */
    int64_t m0 = cx_scale(mach_absolute_time());
    int rt_rc = clock_gettime(CLOCK_REALTIME, &rt);
    int state = ntp_gettime(&ntv);
    int adj = ntp_adjtime(&tx);
    int64_t boottime = cx_scale(mach_continuous_time());
    int64_t m1 = cx_scale(mach_absolute_time());
    if (rt_rc != 0 || state < 0 || adj < 0 || tx.maxerror < 0
            || sysctlbyname("kern.bootsessionuuid", boot, &boot_len, NULL, 0) != 0 || boot_len < 2)
        return PyErr_Format(PyExc_OSError, "clock_unavailable");
    return Py_BuildValue("{s:s,s:L,s:L,s:L,s:L,s:i,s:i,s:l,s:s}",
        "platform", "darwin", "m0", (long long)m0, "m1", (long long)m1,
        "realtime_ns", (long long)rt.tv_sec * 1000000000LL + rt.tv_nsec,
        "boottime_ns", (long long)boottime, "ntp_state", state, "ntp_status", tx.status,
        "maxerror_us", tx.maxerror, "boot_id", boot);
}

static PyMethodDef cx_methods[] = {
    {"clock_sample", cx_clock_sample, METH_NOARGS, "Bracketed kernel clock sample (DATA)."},
    {NULL, NULL, 0, NULL}};

static struct PyModuleDef cx_module = {PyModuleDef_HEAD_INIT, "_macos_native", NULL, -1, cx_methods, NULL, NULL, NULL, NULL};

PyMODINIT_FUNC PyInit__macos_native(void) {
    if (mach_timebase_info(&cx_timebase) != KERN_SUCCESS || cx_timebase.denom == 0) {
        PyErr_SetString(PyExc_OSError, "clock_unavailable");
        return NULL;
    }
    return PyModule_Create(&cx_module);
}
