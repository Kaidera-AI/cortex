"""API image inputs, one tool (J01): `receipt <archive>` writes ../inputs-receipt.json from the archive's wheels; `verify <archive>` checks archive bytes against
SHA256SUMS, the receipt and both locks; `smoke <image>` runs the import smoke in a built image. Build-time checks, not tests; exit 0 = ok. Stdlib only."""
import hashlib, json, re, subprocess, sys, zipfile
from email.parser import BytesParser
from pathlib import Path

HERE = Path(__file__).resolve().parent
RECIPE = HERE.parent
ARCHES = ('amd64', 'arm64')
LINE = re.compile(r'^([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9_.+!-]*) --hash=sha256:([0-9a-f]{64})$')


def norm(name):
    return name.lower().replace('_', '-')


def sha(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def lock(arch):
    rows = {}
    for raw in (RECIPE / f'requirements.linux-{arch}.lock').read_text().splitlines():
        m = LINE.match(raw)
        if m:
            rows[norm(m.group(1))] = (m.group(2), m.group(3))
    return rows


def applies(marker):
    """Requirement marker evaluated for CPython 3.14 on Linux: extras and python<3.14 and PyPy-only requirements do not apply."""
    if not marker:
        return True
    if 'extra' in marker or re.search(r"(sys_platform|platform_system|os_name)\s*==\s*['\"](win32|Windows|nt)['\"]", marker):
        return False
    m = re.search(r"python_(?:full_)?version\s*<\s*['\"]3\.(\d+)", marker)
    return not (m and int(m.group(1)) <= 14)


def metadata(path):
    with zipfile.ZipFile(path) as z:
        name = next(n for n in z.namelist() if n.endswith('.dist-info/METADATA'))
        msg = BytesParser().parsebytes(z.read(name))
    licence = msg.get('License-Expression') or msg.get('License') or ''
    if not licence or len(licence) > 60:
        classes = [c.split('::')[-1].strip() for c in msg.get_all('Classifier', []) if c.startswith('License ::')]
        licence = ' / '.join(classes) or licence[:60]
    requires = []
    for r in msg.get_all('Requires-Dist', []):
        req, _, marker = r.partition(';')
        if applies(marker.strip()):
            requires.append(norm(re.split(r'[ <>=!~(\[]', req.strip())[0]))
    return msg['Name'], msg['Version'], licence, requires


def receipt(archive):
    archive = Path(archive)
    sources = {r['path']: r for r in json.loads((archive / 'RECEIPT.json').read_text())['files']}
    out = {'schema': 'cortex.api-inputs-receipt.v1', 'interpreter': 'CPython 3.14 musllinux_1_2', 'archive_sums_sha256': sha(archive / 'SHA256SUMS'), 'wheels': {}}
    for arch in ARCHES:
        locked = {h: n for n, (_, h) in lock(arch).items()}
        rows = []
        for path in sorted((archive / f'linux-{arch}' / 'assets' / 'wheels').glob('*.whl')):
            digest = sha(path)
            if digest not in locked:
                continue
            name, version, licence, requires = metadata(path)
            src = sources[f'linux-{arch}/assets/wheels/{path.name}']
            rows.append({'file': path.name, 'name': name, 'version': version, 'sha256': digest, 'bytes': src['bytes'], 'licence': src.get('licence') or licence,
                         'requires': requires, 'source': src['source']})
        out['wheels'][arch] = rows
    (RECIPE / 'inputs-receipt.json').write_text(json.dumps(out, indent=1, sort_keys=True) + '\n')
    print(json.dumps({a: len(r) for a, r in out['wheels'].items()}))


def verify(archive):
    archive, bad = Path(archive), []
    for line in (archive / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ', 1)
        if not (archive / name).is_file() or sha(archive / name) != digest:
            bad.append('archive:' + name)
    rec = json.loads((RECIPE / 'inputs-receipt.json').read_text())
    for arch in ARCHES:
        files = {sha(p): p.name for p in (archive / f'linux-{arch}' / 'assets' / 'wheels').glob('*.whl')}
        locked = {h for _, h in lock(arch).values()}
        bad += [f'{arch}:locked hash with no wheel {h[:12]}' for h in sorted(locked - set(files))]
        bad += [f'{arch}:wheel with no lock line {files[h]}' for h in sorted(set(files) - locked)]
        bad += [f'{arch}:receipt disagrees {r["name"]}' for r in rec['wheels'][arch] if lock(arch).get(norm(r['name'])) != (r['version'], r['sha256'])]
    print(json.dumps({'problems': bad}))
    return not bad


SMOKE = ('import jsonschema, psycopg, asyncpg, uvicorn, starlette, httpx, cortex_core.runtime, cortex_core.runtime.pg_adapters\n'
         'import cryptography, cffi\n'
         'from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey\n'
         'from cortex_core.release.ed25519 import verify_ed25519\n'
         'k = Ed25519PrivateKey.generate(); m = b"a-day1"; s = k.sign(m)\n'
         'pub = k.public_key().public_bytes_raw()\n'
         'assert verify_ed25519(pub, m, s) is True, "valid signature refused"\n'
         'assert verify_ed25519(pub, m + b"x", s) is False, "tampered message accepted"\n'
         'print(asyncpg.__version__, cryptography.__version__)\n')


def smoke(image):
    r = subprocess.run(['podman', 'run', '--rm', '--network=none', '--entrypoint', '/opt/cortex/python/bin/python3', image, '-I', '-c', SMOKE],
                       capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip()[-400:])
    return r.returncode == 0 and r.stdout.strip() == '0.32.0 50.0.0'


if __name__ == '__main__':
    cmd, arg = sys.argv[1:3]
    ok = {'receipt': lambda a: receipt(a) or True, 'verify': verify, 'smoke': smoke}[cmd](arg)
    sys.exit(0 if ok else 1)
