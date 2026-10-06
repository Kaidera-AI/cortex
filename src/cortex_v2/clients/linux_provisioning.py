"""Finite Linux host process port. Private frames never enter argv or logs."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import pwd
import selectors
import signal
import subprocess
import time

from .native_prerequisite import PrerequisiteRefusal, strict_json
from .provisioning import ProvisionRefusal


REFUSALS = frozenset({
    'cortex_health_unavailable', 'cortex_health_degraded', 'cortex_credential_refused',
    'cortex_credential_unavailable', 'cortex_provisioning_reissue_required',
    'cortex_provisioning_setup_required', 'cortex_provisioning_owner_required',
    'cortex_provisioning_conflict', 'cortex_descriptor_invalid',
    'cortex_descriptor_owner_mismatch', 'cortex_instance_mismatch',
    'cortex_release_unsupported', 'cortex_release_signature_invalid',
    'cortex_podman_denied', 'cortex_podman_unsupported', 'cortex_image_mismatch',
})


def _one_frame(raw: bytes, limit: int) -> dict:
    if (not raw.endswith(b'\n') or raw.count(b'\n') != 1
            or not raw.startswith(b'{') or raw[:-1].strip() != raw[:-1]):
        raise ProvisionRefusal('cortex_health_unavailable')
    try:
        value = strict_json(raw, limit=limit)
        # Escaped unpaired surrogates cannot become a valid UTF-8 response.
        json.dumps(value, ensure_ascii=False, allow_nan=False).encode('utf-8')
        return value
    except (PrerequisiteRefusal, ValueError, UnicodeError, TypeError, RecursionError):
        raise ProvisionRefusal('cortex_health_unavailable') from None


def private_json_command(command: list[str], frame: dict, *, timeout: float = 5) -> dict:
    """One private request/response with an absolute work and cleanup deadline.

    The caller binds the executable and owned object before calling this port.
    This function has no ambient credentials, engine defaults or retry.
    """
    try:
        if (not isinstance(command, list) or not 1 <= len(command) <= 64
                or any(not isinstance(arg, str) or '\x00' in arg for arg in command)
                or not Path(command[0]).is_absolute()
                or not isinstance(frame, dict) or type(timeout) not in (int, float)
                or not math.isfinite(timeout) or not 0 < timeout <= 5):
            raise ValueError
        request = json.dumps(frame, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode() + b'\n'
        if len(request) > 65536: raise ValueError
        strict_json(request)
    except (ValueError, TypeError, UnicodeError, PrerequisiteRefusal, RecursionError):
        raise ProvisionRefusal('cortex_provisioning_setup_required') from None
    absolute_deadline = time.monotonic() + timeout
    cleanup_budget = min(.1, timeout / 4)
    work_deadline = absolute_deadline - cleanup_budget
    environment = {'PATH': '/usr/bin:/bin:/usr/sbin:/sbin', 'LC_ALL': 'C',
                   'HOME': pwd.getpwuid(os.getuid()).pw_dir,
                   'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUTF8': '1'}
    process = None
    streams_closed = False
    output, errors = bytearray(), bytearray()
    try:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, env=environment, close_fds=True,
                                   start_new_session=True)
        with selectors.DefaultSelector() as selector:
            for stream, name, event in ((process.stdin, 'input', selectors.EVENT_WRITE),
                                        (process.stdout, 'output', selectors.EVENT_READ),
                                        (process.stderr, 'error', selectors.EVENT_READ)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, event, name)
            position = 0
            while selector.get_map() or process.poll() is None:
                remaining = work_deadline - time.monotonic()
                if remaining <= 0: raise ProvisionRefusal('cortex_health_unavailable')
                for key, _ in selector.select(min(remaining, .05)):
                    stream = key.fileobj
                    if key.data == 'input':
                        try:
                            written = os.write(stream.fileno(), request[position:position + 4096])
                        except BlockingIOError:
                            continue
                        except BrokenPipeError:
                            selector.unregister(stream); stream.close()
                            continue
                        position += written
                        if position == len(request):
                            selector.unregister(stream); stream.close()
                    else:
                        try: chunk = os.read(stream.fileno(), 8192)
                        except BlockingIOError: continue
                        if not chunk:
                            selector.unregister(stream); stream.close()
                            continue
                        buffer, limit = (output, 65536) if key.data == 'output' else (errors, 4096)
                        buffer.extend(chunk)
                        if len(buffer) > limit:
                            raise ProvisionRefusal('cortex_health_unavailable')
            streams_closed = True
        if process.returncode == 0 and not errors:
            return _one_frame(bytes(output), 65536)
        if process.returncode == 2 and not output:
            refusal = _one_frame(bytes(errors), 4096)
            code = refusal.get('code')
            if isinstance(code, str) and code in REFUSALS:
                # Child messages, timestamps and all other fields are discarded.
                raise ProvisionRefusal(code)
        raise ProvisionRefusal('cortex_health_unavailable')
    except ProvisionRefusal:
        raise
    except (OSError, ValueError, TypeError, subprocess.SubprocessError):
        raise ProvisionRefusal('cortex_health_unavailable') from None
    finally:
        if process is not None:
            if not streams_closed:
                try: os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                try: process.wait(timeout=max(0, absolute_deadline - time.monotonic()))
                except subprocess.TimeoutExpired: pass
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None: stream.close()
