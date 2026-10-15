"""One canonical text projection for released memory and session payloads."""
from cortex_core.runtime.contracts import decode
from cortex_core.runtime.signed_member import Refusal

MAX_RAW_BYTES=8*1024*1024
MAX_TEXT_BYTES=16*1024


class UnsupportedText(ValueError):
    code='unsupported_text'

    def __init__(self, reason):
        self.reason=reason
        super().__init__(reason)


def project_text(kind, payload: bytes) -> str:
    if kind not in ('memory','session'):
        raise UnsupportedText('unknown_kind')
    if type(payload) is not bytes:
        raise UnsupportedText('invalid_payload')
    if len(payload)>MAX_RAW_BYTES:
        raise UnsupportedText('text_too_large')
    try:
        body=decode(payload,limit=MAX_RAW_BYTES,text_payload=True)
    except Refusal:
        raise UnsupportedText('invalid_payload') from None
    if not isinstance(body,dict):
        raise UnsupportedText('invalid_payload')
    if kind=='memory':
        if not all(isinstance(body.get(k),str) for k in ('section','content')):
            raise UnsupportedText('invalid_payload')
        text=body['section']+'\n'+body['content']
    else:
        messages=body.get('messages')
        if not isinstance(messages,list):
            raise UnsupportedText('invalid_payload')
        parts=[]
        for message in messages:
            if (not isinstance(message,dict) or message.get('role') not in ('human','agent','system')
                    or not isinstance(message.get('content'),str)):
                raise UnsupportedText('invalid_payload')
            parts.append(message['role']+': '+message['content'])
        text='\n'.join(parts)
    if not text.strip():
        raise UnsupportedText('invalid_payload')
    if len(text.encode('utf-8'))>MAX_TEXT_BYTES:
        raise UnsupportedText('text_too_large')
    return text
