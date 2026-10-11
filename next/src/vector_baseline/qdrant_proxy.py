"""Stdlib-only worker inside the owned isolated client container; never logs secrets."""
import http.client
import json
import os
from pathlib import Path
import sys

MAX_BYTES = 2 * 1024 * 1024


def serve(connection, key, incoming, outgoing, *, session_pid):
    def emit(value):
        outgoing.write(json.dumps(value, allow_nan=False) + '\n')
        outgoing.flush()
    emit({'kind': 'ready', 'pid': session_pid})
    try:
        while True:
            line = incoming.readline(MAX_BYTES + 1)
            if not line:
                break
            seq = None
            try:
                if len(line.encode()) > MAX_BYTES or not line.endswith('\n'):
                    raise ValueError('oversized protocol frame')
                value = json.loads(line)
                seq = value.get('seq')
                if (type(seq) is not int or seq < 1 or set(value) != {'seq', 'method', 'path', 'body'}
                        or value['method'] not in ('GET', 'PUT', 'POST')
                        or value['path'] != '/' and not value['path'].startswith('/collections/b02')):
                    raise ValueError('invalid owned HTTP operation')
                body = None if value['body'] is None else json.dumps(value['body'], allow_nan=False).encode()
                connection.request(value['method'], value['path'], body=body,
                                   headers={'api-key': key, 'Content-Type': 'application/json'})
                response = connection.getresponse()
                raw = response.read(MAX_BYTES + 1)
                if response.status != 200 or len(raw) > MAX_BYTES:
                    raise RuntimeError('HTTP response refused')
                emit({'seq': seq, 'body': json.loads(raw)})
            except Exception as error:
                connection.close()
                emit({'seq': seq, 'error': type(error).__name__})  # Never error text/body/headers.
    finally:
        connection.close()


def main():
    config = json.loads(Path('/run/secrets/qdrant.yaml').read_text())
    connection = http.client.HTTPConnection('qdrant', 6333, timeout=10)
    serve(connection, config['service']['api_key'], sys.stdin, sys.stdout, session_pid=os.getpid())


if __name__ == '__main__':
    main()
