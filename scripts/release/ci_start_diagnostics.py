"""Private stderr capture only for an explicitly attached owned CI DB start."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tomllib
import unicodedata
from install_candidate import Refusal, namespace, names
from linux_ci_storage import physical_path

MESSAGE = 'owned CI start diagnosis refused'
CREDENTIAL = re.compile(r'(?i)(?:password|passwd|\bpwd\b|secret|token|credential|authorization|bearer|api[_-]?key|private[_-]?key|-----BEGIN|[a-z][a-z0-9+.-]*://|\S+:\S+@\S+)')
OPAQUE = re.compile(r'(?<![a-zA-Z0-9])[a-zA-Z0-9+/_=-]{40,}(?![a-zA-Z0-9])')


def _read(path):
    physical_path(path); info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1
            or info.st_mode & 0o7022 or info.st_size > 65536):
        raise ValueError
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        actual = os.fstat(stream.fileno())
        if (actual.st_dev, actual.st_ino, actual.st_size) != (info.st_dev, info.st_ino, info.st_size): raise ValueError
        value = stream.read(65537)
    if len(value) != info.st_size: raise ValueError
    return value


def _write(path, value):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())


def _public_path(value):
    if (not isinstance(value, str) or not Path(value).is_absolute() or '..' in Path(value).parts
            or '://' in value or any(unicodedata.category(c).startswith('C') for c in value)):
        raise ValueError
    return value


class Diagnosis:
    def __init__(self, engine, record, root, runtime, helpers):
        self.engine, self.record, self.root = engine, dict(record), root
        self.namespace = namespace(record); self.container = self.namespace + '_db'
        self.paths = {'runtime_path': runtime['runtime']['path'], 'conmon_path': runtime['conmon']['path'], 'helper_paths': helpers}
        self.original = engine.run; self.ordinal = 0

    def _error(self, raw, exit_code):
        if exit_code == 0: return None, False, 'none'
        if len(raw) > 65536: return None, True, 'other'
        try:
            text = raw.decode('utf-8')
        except UnicodeError:
            return None, True, 'other'
        line = next((line.lstrip() for line in text.splitlines() if line.lstrip().startswith('Error:')), None)
        if line is None: return None, True, 'other'
        folded = line.casefold()
        category = next((name for name, words in [('network', ('network', 'netavark', 'pasta', 'port')),
                         ('runtime', ('runtime', 'crun', 'conmon')), ('storage', ('storage', 'mount', 'overlay')),
                         ('permission', ('permission', 'denied'))] if any(word in folded for word in words)), 'other')
        checked = unicodedata.normalize('NFKC', line)
        for public in [self.container, self.namespace, self.record['installation'], self.paths['runtime_path'], self.paths['conmon_path'], *self.paths['helper_paths']]:
            checked = checked.replace(public, 'PUBLIC_PATH')
        if (CREDENTIAL.search(checked) or OPAQUE.search(checked)
                or any(unicodedata.category(c).startswith('C') for c in line)):
            return None, True, category
        return line[:400], False, category

    def disclose(self, raw, exit_code):
        value = {'redacted_line': None, 'patterns': [], 'family': 'other'}
        if exit_code == 0 or len(raw) > 65536: return value
        try: text = raw.decode('utf-8')
        except UnicodeError: return value
        line = next((x.lstrip() for x in text.split('\n') if x.lstrip().startswith('Error:')), None)
        if line is None: return value
        folded = line.casefold()
        families = [('secret', ('secret not found', 'secret unsupported', 'unsupported secret', 'no such secret')), ('oci', ('oci', 'runtime create', 'runtime start')),
                    ('conmon', ('conmon',)), ('network', ('netavark', 'pasta')),
                    ('cgroup', ('cgroup',)), ('permission', ('permission denied', 'eacces')),
                    ('missing', ('no such file', 'enoent')), ('storage', ('storage', 'overlay')),
                    ('userns', ('user namespace', 'userns', 'subuid'))]
        value['family'] = next((name for name, words in families if any(w in folded for w in words)), 'other')
        line = line.rstrip('\r')
        if any(unicodedata.category(c).startswith('C') for c in line): return value
        line = unicodedata.normalize('NFKC', line)
        if line.count(chr(34)) % 2 or line.count(chr(39)) % 2: return value
        if re.search(r'(?i)\b(?:bearer|authorization|credential)\b|-----BEGIN', line): return value
        # Protect only complete source-owned object identifiers, never arbitrary prefixes.
        public = [n for _, n in names(self.record)] + [self.namespace, self.record['installation']]
        protected = {}
        for i, name in enumerate(sorted(set(public), key=len, reverse=True)):
            key = f'PUBLICOBJECT{i}X'
            pattern = r'(?<![\w.-])' + re.escape(name) + r'(?![\w.-])'
            if re.search(pattern, line):
                line = re.sub(pattern, key, line); protected[key] = name
        fired = []
        def replace(pattern, name):
            nonlocal line
            def redact(match):
                if name not in fired: fired.append(name)
                return f'<redacted:{name}>'
            line = re.sub(pattern, redact, line)
        # Assignments consume quoted or unquoted complete values, including whitespace around =.
        replace(r"(?i)\b[\w-]*(?:pass(?:word|wd)?|token|secret|key|dsn)[\w-]*\s*=\s*(?:\"[^\"]*\"|'[^']*'|[^\s]+)", 'assignment')
        replace(r'(?i)[a-z][a-z0-9+.-]*://[^\s]+', 'url')
        replace(r'(?<![\w])[a-fA-F0-9]{20,}(?![\w])', 'hex')
        replace(r'(?<![\w])[a-zA-Z0-9+/_-]{20,}={0,2}(?![\w])', 'base64')
        # Unbalanced value delimiters and credential syntax remain fail closed.
        if re.search(r"(?i)\b(?:password|passwd|pwd|token|secret|key|dsn)\s*[:=]", line): return value
        for key, name in protected.items(): line = line.replace(key, name)
        value.update(redacted_line=line[:400], patterns=fired)
        return value

    def run(self, args, **kwargs):
        if list(args) != ['start', self.container] or kwargs:
            return self.original(args, **kwargs)
        self.ordinal += 1
        raw_path = self.root / f'start-{self.ordinal:04d}.stderr'
        fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        exit_code, exception, category = None, None, None
        with os.fdopen(fd, 'wb') as stream:
            try:
                result = subprocess.run(self.engine.prefix + list(args), stdout=subprocess.DEVNULL,
                                        stderr=stream, timeout=90)
                exit_code = result.returncode
            except subprocess.TimeoutExpired:
                exception, category = 'timeout', 'timeout'
            except OSError:
                exception, category = 'unavailable', 'unavailable'
            stream.flush(); os.fsync(stream.fileno())
        with raw_path.open('rb') as stream: raw = stream.read(65537)
        error, redacted, measured_category = self._error(raw, exit_code)
        value = {'schema': 'cortex.ci-start-diagnosis.v1', 'verb': 'start', 'exit': exit_code,
                 'category': category or measured_category, 'container': self.container,
                 **self.paths, 'first_error': error, 'redacted': redacted,
                 **self.disclose(raw, exit_code)}
        _write(self.root / f'start-diagnostic-{self.ordinal:04d}.json', value)
        if exception:
            raise Refusal('Podman start unavailable or timed out; no cleanup performed') from None
        if exit_code != 0:
            raise Refusal('Podman start refused; no cleanup performed') from None
        return str(exit_code)

    def cleanup(self, result=None, *, error=None):
        value = {'schema': 'cortex.ci-cleanup.v1', 'installation': self.record['installation'],
                 'namespace': self.namespace, 'source_sha': self.record['source_sha'], 'version': self.record['version']}
        if error is not None or not isinstance(result, dict) or result.get('status') != 'erased':
            value.update(status='FAIL', category='cleanup_refused', redacted=True)
        else:
            value.update(status=result['status'])
            for key in ('removed', 'retained_shared_images', 'root'):
                if key in result: value[key] = result[key]
        _write(self.root / 'cleanup-receipt.json', value)


def attach(engine, record, root, *, runtime_receipt, config):
    try:
        root = Path(root); physical_path(root.parent)
        if os.path.lexists(root) or os.getuid() == 0 or os.geteuid() != os.getuid(): raise ValueError
        runtime = json.loads(_read(Path(runtime_receipt)))
        raw = _read(Path(config)); parsed = tomllib.loads(raw.decode())
        if (runtime['schema'] != 'cortex.native-runtime-preflight.v1' or runtime['status'] != 'PASS'
                or runtime['config_sha256'] != hashlib.sha256(raw).hexdigest()
                or runtime['runtime']['name'] != 'crun' or engine.architecture != 'amd64'
                or len(engine.prefix) != 2 or Path(engine.prefix[0]).name != 'podman'
                or engine.prefix[1] != '--remote=false'):
            raise ValueError
        _public_path(runtime['runtime']['path']); _public_path(runtime['conmon']['path'])
        helpers = parsed['engine']['helper_binaries_dir']
        if not isinstance(helpers, list) or not helpers: raise ValueError
        for path in helpers: _public_path(path)
        # Validate identity before creating capture custody or replacing run.
        namespace(record)
        if re.fullmatch(r'[a-f0-9]{40}', record['source_sha']) is None: raise ValueError
        root.mkdir(mode=0o700)
        diagnosis = Diagnosis(engine, record, root, runtime, helpers)
        engine.run = diagnosis.run
        return diagnosis
    except Exception:
        raise RuntimeError(MESSAGE) from None
