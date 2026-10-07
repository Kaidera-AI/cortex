"""Native URL query preserves the frozen shell's quote(value, safe='/')."""
from types import SimpleNamespace
from cortex_v2.cli.agent_boot import prepare


def test_original_query_encoding_preserves_unicode_space_and_slash():
    args=SimpleNamespace(agent='KAI@helix:PUBLIC',budget='50',full=True,query='PUBLIC / é+?')
    agent,project,path=prepare(args)
    assert (agent,project)==('kai','helix')
    assert path=='/boot/kai?budget=50&full=true&query=PUBLIC%20/%20%C3%A9%2B%3F'
