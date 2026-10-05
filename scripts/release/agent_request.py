"""Native agent bridge freeze entrypoint; no target Python installation."""
from cortex_v2.cli.agent_request import main

if __name__ == "__main__":
    raise SystemExit(main())
