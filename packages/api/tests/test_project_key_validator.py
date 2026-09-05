"""validate_project_key must accept a digit-leading key.

The registered project '2nd-brain' starts with a digit. The project-key validator
required a letter start (`^[a-z]...`) while the role-slug validator one line below
already allowed `^[a-z0-9]...`, so '2nd-brain' boot worked but every endpoint that
called validate_project_key (e.g. /writers) 400'd it. The validator now matches the
role-slug one — leading digit allowed.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from fastapi import HTTPException


@pytest.fixture
def api_module():
    src = Path(__file__).resolve().parent.parent / "main.py"
    spec = importlib.util.spec_from_file_location("cortex_api_pkv_under_test", src)
    assert spec and spec.loader, f"could not load spec for {src}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_accepts_digit_leading_key(api_module):
    assert api_module.validate_project_key("2nd-brain") == "2nd-brain"
    assert api_module.validate_project_key("kaidera-os") == "kaidera-os"


def test_still_rejects_genuinely_invalid_keys(api_module):
    for bad in ("-leading-hyphen", "has space", "", "a"):  # hyphen-start / space / empty / too short
        with pytest.raises(HTTPException):
            api_module.validate_project_key(bad)
