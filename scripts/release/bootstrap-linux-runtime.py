"""Build the pinned native CI runtime in a fresh prefix; never on a target."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

INPUTS = {
    'target': 'linux-x86_64', 'maximum_glibc': '2.35',
    'python': {
        'version': '3.12.14',
        'url': 'https://www.python.org/ftp/python/3.12.14/Python-3.12.14.tar.xz',
        'sha256': '5c8462af5790baf43a321a1559dbe0db06d1be4300fb85fb53c40060668e548a',
        'directory': 'Python-3.12.14', 'license': 'LICENSE',
    },
    'openssl': {
        'version': '3.5.8',
        'url': 'https://www.openssl.org/source/openssl-3.5.8.tar.gz',
        'sha256': 'a8f84a39918ec6415ce765d9b429d313ba97b8143169c172e734b9514464f5b2',
        'directory': 'openssl-3.5.8', 'license': 'LICENSE.txt',
    },
}


def verify_archive(path: Path, expected: str) -> None:
    checksum = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            checksum.update(chunk)
    actual = checksum.hexdigest()
    if actual != expected:
        raise RuntimeError('native runtime source archive digest mismatch')


def build_environment(output: Path, inherited: dict[str, str]) -> dict[str, str]:
    env = dict(inherited)
    for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'CPATH', 'C_INCLUDE_PATH',
                'CPLUS_INCLUDE_PATH', 'LIBRARY_PATH', 'LDFLAGS', 'CFLAGS', 'CXXFLAGS',
                'CPPFLAGS', 'SDKROOT', 'ARCHFLAGS', 'CONFIG_SITE', 'PKG_CONFIG_PATH',
                'LD_PRELOAD', 'LD_LIBRARY_PATH', 'LD_AUDIT', 'LD_RUN_PATH',
                'CC', 'CXX', 'AR', 'RANLIB', 'CROSS_COMPILE', 'PERL5LIB',
                'DYLD_LIBRARY_PATH', 'DYLD_FALLBACK_LIBRARY_PATH', 'MACOSX_DEPLOYMENT_TARGET'):
        env.pop(key, None)
    env.update(PATH='/usr/bin:/bin:/usr/sbin:/sbin', TMPDIR=str(output), LC_ALL='C',
               PKG_CONFIG='/usr/bin/false', SSL_CERT_FILE='/etc/ssl/certs/ca-certificates.crt',
               PIP_CERT='/etc/ssl/certs/ca-certificates.crt',
               LD_LIBRARY_PATH=str(output / 'openssl/lib') + ':' + str(output / 'python/lib'))
    return env


def build(output: Path) -> None:
    if platform.system() != 'Linux' or platform.machine() != 'x86_64':
        raise RuntimeError('native Linux x86_64 builder required')
    libc, version = platform.libc_ver()
    if libc != 'glibc' or tuple(int(v) for v in version.split('.')) > (2, 35):
        raise RuntimeError('native builder glibc must be 2.35 or lower')
    release = platform.freedesktop_os_release()
    if release.get('ID') != 'ubuntu' or release.get('VERSION_ID') != '22.04':
        raise RuntimeError('the admitted builder is Ubuntu 22.04')
    source = Path(__file__).resolve().parents[2]
    if not output.is_absolute() or output.exists() or output.is_symlink():
        raise RuntimeError('fresh absolute native runtime output required')
    if source == output.resolve() or source in output.resolve().parents:
        raise RuntimeError('native runtime output must be outside the checkout')
    if any(parent.is_symlink() for parent in output.parents):
        raise RuntimeError('native runtime output ancestors must be physical directories')
    if not Path('/etc/ssl/certs/ca-certificates.crt').is_file():
        raise RuntimeError('public system CA bundle missing')
    output.mkdir(parents=True)
    env = build_environment(output, os.environ)

    def run(args: list[str], cwd: Path = output, *, read=False) -> str:
        result = subprocess.run(args, cwd=cwd, env=env, check=True, text=True,
                                stdout=subprocess.PIPE if read else None)
        return result.stdout.strip() if read else ''

    # Only authenticated, hash-pinned upstream bytes are extracted/executed.
    for item in (INPUTS['openssl'], INPUTS['python']):
        archive = output / item['url'].rsplit('/', 1)[-1]
        run(['/usr/bin/curl', '--fail', '--location', '--proto', '=https',
             '--proto-redir', '=https', '--tlsv1.2', '--output', str(archive), item['url']])
        verify_archive(archive, item['sha256'])
        run(['/usr/bin/tar', '-xf', str(archive), '-C', str(output)])

    openssl = output / INPUTS['openssl']['directory']
    python = output / INPUTS['python']['directory']
    ssl_prefix, py_prefix = output / 'openssl', output / 'python'
    run(['./Configure', 'linux-x86_64', 'shared', 'no-tests', '--libdir=lib',
         '--prefix=' + str(ssl_prefix), '--openssldir=' + str(ssl_prefix)], openssl)
    run(['/usr/bin/make', '-j2'], openssl)
    run(['/usr/bin/make', 'install_sw'], openssl)
    run(['./configure', '--prefix=' + str(py_prefix), '--enable-shared',
         'LDFLAGS=-Wl,-rpath,' + str(py_prefix / 'lib'),
         '--without-static-libpython', '--with-pkg-config=no', '--without-readline',
         '--disable-test-modules', '--with-openssl=' + str(ssl_prefix),
         '--with-openssl-rpath=auto'], python)
    run(['/usr/bin/make', '-j2'], python)
    run(['/usr/bin/make', 'install'], python)
    interpreter = py_prefix / 'bin/python3'
    actual = json.loads(run([str(interpreter), '-I', '-c',
        'import json,platform,ssl; print(json.dumps(dict(python=platform.python_version(), architecture=platform.machine(), openssl=ssl.OPENSSL_VERSION)))'], read=True))
    if actual['python'] != '3.12.14' or actual['architecture'] != 'x86_64' or not actual['openssl'].startswith('OpenSSL 3.5.8 '):
        raise RuntimeError('compiled native runtime identity mismatch')
    run([str(interpreter), '-I', '-m', 'venv', str(output / 'venv')])
    licenses = output / 'licenses'
    licenses.mkdir()
    for kind in ('python', 'openssl'):
        item = INPUTS[kind]
        shutil.copyfile(output / item['directory'] / item['license'], licenses / (kind + '-' + item['license']))
    inventory = {'schema': 'cortex.native-ci-runtime.v1', 'inputs': INPUTS,
                 'runtime': actual, 'compiler': run(['/usr/bin/gcc', '--version'], read=True).splitlines()[0],
                 'glibc': {'name': libc, 'version': version}, 'os_release': release,
                 'licenses': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in licenses.iterdir()},
                 'scope': 'native builder source closure; not product install or signing'}
    (output / 'runtime-inventory.json').write_text(json.dumps(inventory, indent=2) + '\n')
    print(json.dumps(inventory))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--describe', action='store_true')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.describe:
        print(json.dumps(INPUTS, sort_keys=True))
    elif args.output is None:
        parser.error('--output required for the native build')
    else:
        build(args.output)


if __name__ == '__main__':
    main()
