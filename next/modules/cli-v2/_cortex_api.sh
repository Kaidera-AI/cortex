#!/usr/bin/env bash
# MIT. Sole packaged transport: Core private projection -> stock curl.
# Core alone admits config, resolves custody and decrypts credentials.
set -euo pipefail
case "$-" in *x*|*a*) return 1 2>/dev/null || exit 1 ;; esac
unset _CORTEX_READY _CORTEX_TOKEN _CORTEX_URL _CORTEX_INSTALLATION
unset _CORTEX_PROJECT_ID _CORTEX_PROJECT _CORTEX_SECONDS _CORTEX_PROTOCOL
_CORTEX_READY=0

_cortex_read_projection() {
    python3 -S - "${CORTEX_CLIENT_PROJECTION_FD:-}" <<'PY'
import fcntl, ipaddress, os, re, selectors, stat, sys, time
from urllib.parse import urlsplit
from uuid import UUID
try:
    selected = sys.argv[1]
    if re.fullmatch(r'[0-9]{1,6}', selected) is None or int(selected) < 3:
        raise ValueError
    fd = int(selected)
    info = os.fstat(fd)
    if (not stat.S_ISFIFO(info.st_mode) or info.st_uid != os.geteuid()
            or info.st_nlink != 0 or fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE != os.O_RDONLY):
        raise ValueError
    deadline, raw = time.monotonic() + 1, bytearray()
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_READ)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not selector.select(remaining):
                raise ValueError
            part = os.read(fd, 4096)
            if not part:
                break
            raw.extend(part)
            if len(raw) > 8192:
                raise ValueError
    fields = bytes(raw).split(b'\0')
    if len(fields) != 8 or fields[-1] != b'':
        raise ValueError
    values = [field.decode('utf-8', errors='strict') for field in fields[:-1]]
    if any(not value or any(ord(c) < 32 or ord(c) == 127 for c in value) for value in values):
        raise ValueError
    protocol, url, installation, project, name, credential, seconds = values
    if protocol != 'cortex.client.v1':
        raise ValueError
    parsed = urlsplit(url)
    host = ipaddress.ip_address(parsed.hostname)
    if (parsed.scheme not in ('http', 'https') or not host.is_loopback
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in ('', '/') or parsed.query or parsed.fragment
            or parsed.port is None or not 1 <= parsed.port <= 65535):
        raise ValueError
    expected_host = '[' + str(host) + ']' if host.version == 6 else str(host)
    if parsed.netloc != expected_host + ':' + str(parsed.port):
        raise ValueError
    for value in (installation, project):
        if str(UUID(value)) != value or UUID(value).int == 0:
            raise ValueError
    if (installation == project or re.fullmatch(r'[a-zA-Z_][a-zA-Z_0-9-]{0,62}', name) is None
            or re.fullmatch(r'[0-9a-f]{64}', credential) is None
            or re.fullmatch(r'(?:[1-9]|10)', seconds) is None):
        raise ValueError
    # Full validation and EOF precede every output byte.
    sys.stdout.buffer.write(bytes(raw))
except (OSError, ValueError, TypeError, UnicodeError):
    raise SystemExit(1) from None
PY
}

if [ -n "${CORTEX_CLIENT_PROJECTION_FD:-}" ]; then
    if {
        IFS= read -r -d '' _CORTEX_PROTOCOL &&
        IFS= read -r -d '' _CORTEX_URL &&
        IFS= read -r -d '' _CORTEX_INSTALLATION &&
        IFS= read -r -d '' _CORTEX_PROJECT_ID &&
        IFS= read -r -d '' _CORTEX_PROJECT &&
        IFS= read -r -d '' _CORTEX_TOKEN &&
        IFS= read -r -d '' _CORTEX_SECONDS
    } < <(_cortex_read_projection); then
        _CORTEX_READY=1
        CORTEX_API="${_CORTEX_URL%/}"
        CORTEX_PROJECT="${_CORTEX_PROJECT}"
        CORTEX_PROJECT_ID="${_CORTEX_PROJECT_ID}"
    fi
fi

_cortex_transport_refusal() {
    printf '%s\n' '{"error":{"code":"unavailable","reason":"client_projection_required","retryable":false}}' >&2
    return 1
}
_cortex_projection() {
    builtin printf '%s\0' "${_CORTEX_PROTOCOL}" "${_CORTEX_URL}" "${_CORTEX_INSTALLATION}" \
        "${_CORTEX_PROJECT_ID}" "${_CORTEX_PROJECT}" "${_CORTEX_TOKEN}" "${_CORTEX_SECONDS}"
}

# No caller curl flags. A mutation requires its caller's stable, nonsecret key.
# This helper never creates a key or retries a request.
cortex_api_call() {
    case "$-" in *x*|*a*) return 1 ;; esac
    if [ "${_CORTEX_READY:-0}" != 1 ] || [ "$#" -lt 2 ] || [ "$#" -gt 4 ] \
       || [ -n "${CORTEX_API_PAYLOAD_FILE:-}" ]; then
        _cortex_transport_refusal
        return 1
    fi
    python3 -S - "${1}" "${2}" "${4:-}" "${CORTEX_IDEMPOTENCY_KEY:-}" \
        3< <(_cortex_projection) 4< <(builtin printf '%s' "${3:-}") <<'PY'
import json, os, re, selectors, signal, subprocess, sys, time
from uuid import UUID
MAX_BYTES = 1048576

class Refusal(Exception):
    def __init__(self, reason):
        self.reason = reason

def fail(reason):
    raise Refusal(reason)

def cancelled(_number, _frame):
    fail('cancelled')

signal.signal(signal.SIGTERM, cancelled)
signal.signal(signal.SIGINT, cancelled)

def unique(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            fail('invalid_response')
        value[key] = item
    return value

def decode(raw):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=unique,
                           parse_constant=lambda _value: fail('invalid_response'))
    except (ValueError, UnicodeError, RecursionError):
        fail('invalid_response')
    if type(value) is not dict:
        fail('invalid_response')
    return value

def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'

def uuid(value):
    try:
        return type(value) is str and str(UUID(value)) == value and UUID(value).int != 0
    except (ValueError, AttributeError):
        return False

def bounded_read(fd, maximum):
    raw = bytearray()
    while True:
        part = os.read(fd, 4096)
        if not part:
            return bytes(raw)
        raw.extend(part)
        if len(raw) > maximum:
            fail('input_too_large')

def request(url, method, headers, body, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        fail('deadline')
    config = ['silent', 'globoff', 'path-as-is', 'proxy = ""', 'noproxy = "*"',
              'proto = "=http,https"', 'max-redirs = 0',
              'connect-timeout = ' + str(remaining), 'max-time = ' + str(remaining),
              'max-filesize = ' + str(MAX_BYTES), 'request = ' + quote(method),
              'url = ' + quote(url), 'write-out = "\\n%{http_code}"']
    config.extend('header = ' + quote(name + ': ' + value) for name, value in headers.items())
    if body:
        config += ['header = "Content-Type: application/json"', 'data-binary = "@-"']
    config_bytes = ('\n'.join(config) + '\n').encode('utf-8')
    config_read, config_write = os.pipe()
    process = None
    try:
        process = subprocess.Popen(['/usr/bin/curl', '--disable', '--config', '/dev/fd/' + str(config_read)],
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env={'LC_ALL': 'C'}, pass_fds=(config_read,))
        os.close(config_read)
        config_read = None
        raw = bytearray()
        with selectors.DefaultSelector() as selector:
            for stream, data in ((config_write, config_bytes), (process.stdin, body)):
                fd = stream if type(stream) is int else stream.fileno()
                if data:
                    os.set_blocking(fd, False)
                    selector.register(stream, selectors.EVENT_WRITE, memoryview(data))
                elif type(stream) is int:
                    os.close(stream)
                    config_write = None
                else:
                    stream.close()
            selector.register(process.stdout, selectors.EVENT_READ)
            done = False
            while not done:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    fail('deadline')
                events = selector.select(remaining)
                if not events:
                    fail('deadline')
                for event, _ in events:
                    if event.events == selectors.EVENT_WRITE:
                        stream = event.fileobj
                        fd = stream if type(stream) is int else stream.fileno()
                        rest = event.data[os.write(fd, event.data):]
                        if rest:
                            selector.modify(stream, selectors.EVENT_WRITE, rest)
                        else:
                            selector.unregister(stream)
                            if type(stream) is int:
                                os.close(stream)
                                config_write = None
                            else:
                                stream.close()
                    else:
                        part = os.read(process.stdout.fileno(), 65536)
                        if not part:
                            done = True
                            break
                        raw.extend(part)
                        if len(raw) > MAX_BYTES + 4:
                            fail('response_too_large')
        if process.wait(timeout=max(0.001, deadline - time.monotonic())) != 0:
            fail('transport')
        body_bytes, code = bytes(raw).rsplit(b'\n', 1)
        if len(body_bytes) > MAX_BYTES or re.fullmatch(rb'[1-5][0-9]{2}', code) is None:
            fail('invalid_response')
        if credential.encode() in body_bytes:
            fail('secret_in_response')
        return int(code), decode(body_bytes)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        fail('transport')
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
            if not process.stdin.closed:
                process.stdin.close()
        if config_read is not None:
            os.close(config_read)
        if config_write is not None:
            os.close(config_write)

try:
    fields = bounded_read(3, 8192).split(b'\0')
    if len(fields) != 8 or fields[-1] != b'':
        fail('client_projection_required')
    protocol, base, installation, project_id, project, credential, seconds = [v.decode('utf-8') for v in fields[:-1]]
    deadline = time.monotonic() + int(seconds)
    method, path, agent, key = sys.argv[1:]
    if (method not in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE')
            or not path.startswith('/') or path.startswith('//') or '#' in path or '\\' in path
            or any(ord(c) <= 32 or ord(c) >= 127 for c in path) or len(path) > 8192
            or (agent and re.fullmatch(r'[a-zA-Z0-9_.@:-]{1,192}', agent) is None)):
        fail('request_shape')
    mutation = method != 'GET'
    if mutation and (not agent or re.fullmatch(r'[a-zA-Z0-9_.:@/-]{1,256}', key) is None):
        fail('idempotency_key_required')
    payload = bounded_read(4, MAX_BYTES)
    if payload:
        decode(payload)
    status, health = request(base.rstrip('/') + '/health', 'GET', {}, b'', deadline)
    if (status != 200 or set(health) != {'component', 'status', 'readiness', 'installation_id'}
            or health.get('component') != 'cortex' or health.get('status') != 'ok'
            or health.get('readiness') != 'ready'
            or health.get('installation_id') != installation):
        fail('installation_mismatch')
    headers = {'Authorization': 'Bearer ' + credential, 'X-Project': project, 'X-Project-Id': project_id}
    if agent:
        headers['X-Agent-Name'] = agent
    if mutation:
        headers['Idempotency-Key'] = key
    status, result = request(base.rstrip('/') + path, method, headers, payload, deadline)
    if 300 <= status < 400:
        fail('redirect_refused')
    if status >= 400:
        error = result.get('error')
        if (type(error) is not dict or not set(error) <= {'code', 'capability', 'state', 'reason', 'retryable', 'message'}
                or type(error.get('code')) is not str or not error['code']
                or type(error.get('retryable')) is not bool
                or ('message' in error and (type(error['message']) is not str
                    or len(error['message'].encode('utf-8')) > 256))
                or any(value is not None and type(value) is not str for name, value in error.items() if name != 'retryable')):
            fail('invalid_response')
        print(json.dumps({'error': error}, separators=(',', ':')), file=sys.stderr)
        raise SystemExit(22)
    if not 200 <= status < 300 or 'error' in result:
        fail('invalid_response')
    if mutation:
        receipt = result.get('receipt')
        if (type(receipt) is not dict or set(receipt) != {'record_id', 'revision', 'event_id', 'payload_sha256'}
                or not uuid(receipt['record_id']) or not uuid(receipt['event_id'])
                or type(receipt['revision']) is not int or receipt['revision'] < 1
                or type(receipt['payload_sha256']) is not str
                or re.fullmatch(r'[0-9a-f]{64}', receipt['payload_sha256']) is None):
            fail('committed_receipt_required')
    print(json.dumps(result, separators=(',', ':'), ensure_ascii=False))
except Refusal as error:
    print(json.dumps({'error': {'code': 'unavailable', 'reason': error.reason, 'retryable': False}},
                     separators=(',', ':')), file=sys.stderr)
    raise SystemExit(1) from None
except (OSError, ValueError, UnicodeError):
    print('{"error":{"code":"unavailable","reason":"transport","retryable":false}}', file=sys.stderr)
    raise SystemExit(1) from None
PY
}

cortex_api_json() { cortex_api_call "$@"; }
cortex_api_call_json() { cortex_api_call "$@"; }
cortex_api_call_admin() { _cortex_transport_refusal; }
cortex_api_download_admin() { _cortex_transport_refusal; }
cortex_agent_base_name() { local base="${1%%@*}"; printf '%s' "${base%%:*}"; }
cortex_agent_display_name() { printf '%s@%s' "$(cortex_agent_base_name "$1")" "${CORTEX_PROJECT:-}"; }
cortex_api_urlencode() {
    local s="$1" out='' c n i old_lc="${LC_ALL:-}"
    LC_ALL=C
    for ((i=0; i<${#s}; i++)); do
        c="${s:i:1}"
        case "$c" in
            [a-zA-Z0-9_.~/-]) out+="$c" ;;
            *) printf -v n '%d' "'$c"; printf -v c '%%%02X' "$((n & 0xFF))"; out+="$c" ;;
        esac
    done
    LC_ALL="$old_lc"
    printf '%s' "$out"
}
cortex_api_urlencode_strict() {
    local encoded
    encoded="$(cortex_api_urlencode "$1")"
    printf '%s' "${encoded//\//%2F}"
}
cortex_api() {
    local method="$1" path="$2"; shift 2
    local query='' item
    case "$method" in
        GET)
            for item in "$@"; do query="${query:+${query}&}${item}"; done
            cortex_api_call GET "${path}${query:+?${query}}" '' "${CORTEX_AGENT_ID:-}" ;;
        POST|PUT|PATCH|DELETE) cortex_api_call "$method" "$path" "${1:-}" "${CORTEX_AGENT_ID:-}" ;;
        *) _cortex_transport_refusal ;;
    esac
}
