"""Actual serialized ASCII/UTF-8 protocol byte limits, including final newline."""
import json

import pytest

from test_cm2_linux_cli import product


@pytest.mark.parametrize('case',['exact-ascii','oversize-ascii','exact-utf8','oversize-utf8'])
def test_serialized_public_line_enforces_the_literal_byte_limit_including_newline(case):
    cli=product()
    room=65536-len(b'{"x":""}\n')
    text='a'*room if case.endswith('ascii') else 'é'*(room//2)+'a'*(room%2)
    if case.startswith('oversize'):text+='a'
    if case.startswith('exact'):
        line=cli._public_line({'x':text},65536)
        assert isinstance(line,str) and len(line.encode('utf-8'))==65536 and line.endswith('\n') and line.count('\n')==1
        assert json.loads(line)=={'x':text}
    else:
        with pytest.raises(Exception) as caught:cli._public_line({'x':text},65536)
        assert text not in str(caught.value)
