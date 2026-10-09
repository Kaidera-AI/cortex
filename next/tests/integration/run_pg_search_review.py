"""Adapt the existing disposable PG runner to include review regression cases."""

import subprocess
from unittest.mock import patch
import run_pg_search

original_run = subprocess.run


def with_review_cases(command, **kwargs):
    command = list(command)
    if "test_pg_search.py" in command:
        command[command.index("test_pg_search.py")] = "test_pg_search*.py"
    return original_run(command, **kwargs)


if __name__ == "__main__":
    with patch.object(subprocess, "run", side_effect=with_review_cases):
        raise SystemExit(run_pg_search.main())
