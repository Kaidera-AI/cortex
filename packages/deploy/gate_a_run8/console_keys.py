"""One console-key classifier; zero may wait, conflict/decode failure never retry."""
import base64,re

class ConsoleDecodeStop(ValueError):pass

def decoded_console_text(raw):
    text=raw
    if 'ssh-ed25519 ' not in text:
        try:text=base64.b64decode(text,validate=True).decode('utf-8')
        except (ValueError,UnicodeError) as exc:raise ConsoleDecodeStop('CONSOLE_DECODE_STOP') from exc
    return text

def classify_console_keys(keys):
    unique=set(keys)
    if not unique:return {'status':'WAIT','exit':75}
    if len(unique)!=1:return {'status':'CONFLICT','exit':76}
    return {'status':'ONE','key':keys[0],'exit':0}

def parse_console(raw):
    try:text=decoded_console_text(raw)
    except ConsoleDecodeStop:return {'status':'DECODE_STOP','exit':76}
    keys=re.findall(r'^ssh-ed25519\s+([A-Za-z0-9+/=]+)(?:\s|$)',text,flags=re.M)
    return classify_console_keys(keys)

def bind_with_wait(invoke,retire,clock,sleep):
    start=clock()
    while True:
        result=invoke()
        if result['exit']==0:return result
        if result['exit']!=75 or clock()-start>=600:
            retire();return result
        sleep(60)
