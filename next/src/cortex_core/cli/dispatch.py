"""Public source CLI dispatch; the installer configures CORTEX_API_URL."""


def dispatch(argv, environ, status_main):
    if (argv and argv[0] == "status" and "--endpoint" not in argv
            and isinstance(environ.get("CORTEX_API_URL"), str)
            and environ["CORTEX_API_URL"]):
        argv = ["status", "--endpoint", environ["CORTEX_API_URL"], *argv[1:]]
    return status_main(argv)


def main(argv=None):
    from cortex_core.cli.status import main as status_main
    import os
    import sys
    return dispatch(sys.argv[1:] if argv is None else argv, os.environ, status_main)
