"""Finite AF_UNIX-only owner client. Sensitive bytes use attached pipes only."""
from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path
import sys
from uuid import UUID

import h11
from service_auth_owner import (MAX_BODY, MAX_HEADER, MAX_RESPONSE, PROTOCOL,
    TIMEOUT, OwnerError, decode, encode, peer_uid, private_socket, validate_request)


async def request(path, value, *, timeout=TIMEOUT):
    data = encode(validate_request(value))
    if len(data) > MAX_BODY: raise OwnerError('owner request too large')

    async def exchange():
        private_socket(path)
        reader, writer = await asyncio.open_unix_connection(str(path), limit=MAX_HEADER + MAX_RESPONSE)
        try:
            if peer_uid(writer.get_extra_info('socket')) != os.getuid():
                raise OwnerError('owner server identity refused')
            conn = h11.Connection(h11.CLIENT, max_incomplete_event_size=MAX_HEADER)
            writer.write(conn.send(h11.Request(method='POST', target='/v1/owner', headers=[
                ('Host', 'cortex-owner'), ('Content-Type', 'application/json'), ('Content-Length', str(len(data)))])))
            writer.write(conn.send(h11.Data(data=data)))
            writer.write(conn.send(h11.EndOfMessage()))
            await writer.drain()
            body, count, response = bytearray(), 0, None
            while True:
                event = conn.next_event()
                if event is h11.NEED_DATA:
                    chunk = await reader.read(4096)
                    count += len(chunk)
                    if count > MAX_HEADER + MAX_RESPONSE: raise OwnerError('owner response too large')
                    conn.receive_data(chunk)
                elif isinstance(event, h11.Response):
                    response = event
                    headers = dict(event.headers)
                    if (response.status_code != 200 or headers.get(b'cache-control') != b'no-store'
                            or b'transfer-encoding' in headers or b'content-length' not in headers
                            or int(headers[b'content-length']) > MAX_RESPONSE):
                        raise OwnerError('owner service unavailable; outcome unknown, do not retry automatically')
                elif isinstance(event, h11.Data):
                    body.extend(event.data)
                    if len(body) > MAX_RESPONSE: raise OwnerError('owner response too large')
                elif isinstance(event, h11.EndOfMessage):
                    result = decode(body)
                    if (response is None or conn.trailing_data[0] or not isinstance(result, dict)
                            or set(result) != {'protocol', 'instance_id', 'result'}
                            or result['protocol'] != PROTOCOL or not isinstance(result['result'], dict)
                            or value['instance_id'] is not None and result['instance_id'] != value['instance_id']):
                        raise OwnerError('owner response identity mismatch')
                    instance = result['instance_id']
                    if (not isinstance(instance, str) or str(UUID(instance)) != instance
                            or value['operation'] == 'identity' and result['result'] != {'instance_id': instance}):
                        raise OwnerError('owner response identity mismatch')
                    return result
                else: raise OwnerError('owner response unavailable')
        finally:
            writer.close()
            with contextlib.suppress(Exception): await asyncio.wait_for(writer.wait_closed(), 0.2)

    try:
        return await asyncio.wait_for(exchange(), timeout)
    except asyncio.CancelledError:
        raise
    except Exception:
        raise OwnerError('owner service unavailable; outcome unknown, do not retry automatically') from None


def main():
    # No secret argv, TTY or configurable network URL; image startup is finite.
    if len(sys.argv) != 1 or sys.stdin.isatty() or sys.stdout.isatty():
        raise OwnerError('owner client requires protected input and output streams')
    data = sys.stdin.buffer.read(MAX_BODY + 1)
    if len(data) > MAX_BODY: raise OwnerError('owner input too large')
    result = asyncio.run(request(Path('/run/cortex-owner/owner.sock'), decode(data)))
    data = encode(result)
    if len(data) > MAX_RESPONSE: raise OwnerError('owner output too large')
    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()


if __name__ == '__main__':
    try:
        main()
    except Exception:
        print('owner request unavailable; outcome unknown, do not retry automatically', file=sys.stderr)
        sys.exit(1)
