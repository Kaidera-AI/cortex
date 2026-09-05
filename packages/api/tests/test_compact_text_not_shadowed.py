"""The storage compactor must not be shadowed by the display helper.

`ingest.compact_text` returns tuple[str, bool, int] and feeds the E4 compaction path.
`main.compact_text` is an unrelated display helper returning str, defined LATER in the
same module, so importing the ingest one under its own name let the `def` win for the
whole module. The three E4 call sites then unpacked three values from a str and raised
"ValueError: too many values to unpack (expected 3)".

That went unnoticed for months because E4 is flag-gated off everywhere, so the feature
was simultaneously dead and broken. These tests fail if the names ever collide again.
"""

import inspect

import main


def test_storage_compactor_returns_the_three_tuple_e4_expects():
    text, changed, saved = main.compact_for_storage("a rationale worth compacting")
    assert isinstance(text, str)
    assert isinstance(changed, bool)
    assert isinstance(saved, int)


def test_display_helper_still_returns_a_plain_string():
    assert main.compact_text("  spaced   out  ") == "spaced out"
    assert main.compact_text("abcdefghij", limit=6) == "abc..."


def test_the_two_helpers_are_not_the_same_object():
    # The whole defect was one name bound to both roles.
    assert main.compact_for_storage is not main.compact_text


def test_no_call_site_unpacks_the_display_helper():
    """A tuple unpack of the str helper is the exact crash this guards."""
    import re

    source = inspect.getsource(main)
    offenders = re.findall(r"\w+,\s*\w+,\s*\w+\s*=\s*compact_text\(", source)
    assert not offenders, f"tuple-unpacking the str display helper: {offenders}"
