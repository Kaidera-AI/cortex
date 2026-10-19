"""ship/install sign + installer spine (signature BEFORE any effect). Needs the stock `minisign` binary and POSIX sh (a missing tool FAILS, never skips).
Every test builds its own throwaway key pairs; no key material is committed. A stub `podman` first on PATH records every call, so "no effect before verify"
is observed, not assumed. Source/file-level proof only: nothing here is an installed or paired-candidate credit."""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
NEXT = HERE.parents[3]
WRITER = INSTALL / 'sign/write_release.py'
INSTALL_SH = INSTALL / 'install.sh'
ARCHIVES = {a: [f'cortex-core-linux-{a}.oci.tar', f'cortex-api-linux-{a}.oci.tar'] for a in ('arm64', 'amd64')}
IDENTITY = {'release_id': 'test-release-001', 'release_lineage': 'cortex-v2', 'release_sequence': 1, 'api_contract': 'cortex.api.v1',
            'source_revision': '0' * 40}
STUB = """#!/bin/sh
echo "$*" >> "$STUB_LOG"
case "$1" in
  --version) echo "podman version ${STUB_VERSION:-7.0.3}" ;;
  load) f=""; while [ $# -gt 0 ]; do [ "$1" = "-i" ] && f="$2"; shift; done; echo "Loaded image: sha256:$(shasum -a 256 "$f" | cut -d' ' -f1)" ;;
  *) : ;;
esac
"""


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def minisign_keys(d, name):
    sec, pub = d / f'{name}.sec', d / f'{name}.pub'
    r = sh(['minisign', '-G', '-W', '-f', '-p', str(pub), '-s', str(sec)])
    assert r.returncode == 0, r.stderr
    return sec, pub


class Release:
    """A signed release folder in a temp dir; `arch` is the platform under test."""

    def __init__(self, signed=True, extra=None):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix='nix-sign-')))
        self.dir = self.tmp / 'release'
        self.dir.mkdir()
        self.sec, self.pub = minisign_keys(self.tmp, 'good')
        self.sec2, self.pub2 = minisign_keys(self.tmp, 'other')
        for names in ARCHIVES.values():
            for n in names:
                (self.dir / n).write_bytes(os.urandom(64) + n.encode())
        (self.dir / 'runtime-operations-v1.json').write_bytes((NEXT / 'contracts/runtime/runtime-operations-v1.json').read_bytes())
        for n, src in (('install.sh', INSTALL_SH), ('record.py', INSTALL / 'record.py')):
            (self.dir / n).write_bytes(src.read_bytes() if src.is_file() else b'#!/bin/sh\n')
        for n, data in (extra or {}).items():
            (self.dir / n).write_bytes(data)
        self.identity, self.images = self.tmp / 'identity.json', self.tmp / 'images.json'
        self.identity.write_text(json.dumps(IDENTITY))
        self.images.write_text(json.dumps({f'linux/{a}': {'core': f'localhost/cortex-v2-core:{a}', 'api': f'localhost/cortex-v2-api:{a}'} for a in ARCHIVES}))
        self.written = sh([sys.executable, '-I', str(WRITER), '--dir', str(self.dir), '--identity', str(self.identity), '--images', str(self.images)])
        if signed:
            self.sign(self.sec)

    def sign(self, sec):
        for old in self.dir.glob('*.minisig'):
            old.unlink()
        return sh(['minisign', '-S', '-s', str(sec), '-m', str(self.dir / 'release.json'), str(self.dir / 'SHA256SUMS')])

    def verify(self, pub=None, arch='arm64'):
        return sh(['sh', str(INSTALL_SH), '--verify-only', '--release-dir', str(self.dir), '--trust-key-file', str(pub or self.pub), '--platform', f'linux-{arch}'])

    def cleanup(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


def reason(r):
    try:
        return json.loads(r.stderr.strip().splitlines()[-1])['reason']
    except (ValueError, IndexError, KeyError):
        return f'<no typed refusal: {r.stderr[-200:]!r}>'


class WriteRelease(unittest.TestCase):
    def setUp(self):
        self.r = Release(signed=False)
        self.addCleanup(self.r.cleanup)

    def test_writer_succeeds_on_a_clean_folder(self):
        self.assertEqual(self.r.written.returncode, 0, self.r.written.stderr)

    def test_sums_are_payload_plus_release_json_in_name_order_and_nothing_else(self):
        self.assertEqual(self.r.written.returncode, 0, self.r.written.stderr)
        names = [l.split('  ', 1)[1] for l in (self.r.dir / 'SHA256SUMS').read_text().splitlines()]
        payload = json.loads((self.r.dir / 'release.json').read_text())['files']
        self.assertEqual(names, sorted([f['name'] for f in payload] + ['release.json']))
        self.assertFalse([n for n in names if n == 'SHA256SUMS' or n.endswith('.minisig')])

    def test_sums_agree_with_release_json_and_with_the_files(self):
        self.assertEqual(self.r.written.returncode, 0, self.r.written.stderr)
        lines = dict(reversed(l.split('  ', 1)) for l in (self.r.dir / 'SHA256SUMS').read_text().splitlines())
        for f in json.loads((self.r.dir / 'release.json').read_text())['files']:
            real = hashlib.sha256((self.r.dir / f['name']).read_bytes()).hexdigest()
            self.assertEqual((f['sha256'], lines[f['name']], f['size']), (real, real, (self.r.dir / f['name']).stat().st_size))
        self.assertEqual(lines['release.json'], hashlib.sha256((self.r.dir / 'release.json').read_bytes()).hexdigest())

    def test_release_json_carries_identity_images_and_the_schema(self):
        self.assertEqual(self.r.written.returncode, 0, self.r.written.stderr)
        doc = json.loads((self.r.dir / 'release.json').read_text())
        self.assertEqual(doc['schema'], 'cortex.release.v1')
        for k, v in IDENTITY.items():
            self.assertEqual(doc[k], v)
        self.assertEqual(sorted(doc['images']), ['linux/amd64', 'linux/arm64'])
        self.assertEqual({f['kind'] for f in doc['files']} >= {'image'}, True)

    def test_a_payload_that_names_the_index_files_is_refused_and_nothing_is_written(self):
        for bad in ('release.json', 'SHA256SUMS', 'x.minisig'):
            r = Release(signed=False, extra={bad: b'x'}) if bad != 'release.json' else None
            if r is None:
                continue
            self.addCleanup(r.cleanup)
            self.assertEqual(r.written.returncode, 3, bad)
        r = Release(signed=False)
        self.addCleanup(r.cleanup)
        (r.dir / 'release.json').unlink()
        (r.dir / 'SHA256SUMS').unlink()
        (r.dir / 'stale.minisig').write_bytes(b'x')
        again = sh([sys.executable, '-I', str(WRITER), '--dir', str(r.dir), '--identity', str(r.identity), '--images', str(r.images)])
        self.assertEqual(again.returncode, 3)
        self.assertFalse((r.dir / 'release.json').exists())
        self.assertFalse((r.dir / 'SHA256SUMS').exists())


class VerifyRelease(unittest.TestCase):
    def setUp(self):
        self.r = Release()
        self.addCleanup(self.r.cleanup)
        self.assertEqual(self.r.written.returncode, 0, 'the writer must produce the signed release these tests verify')

    def test_valid_release_is_accepted(self):
        v = self.r.verify()
        self.assertEqual((v.returncode, v.stderr.strip()), (0, ''), v.stderr)

    def test_wrong_key_is_refused(self):
        v = self.r.verify(pub=self.r.pub2)
        self.assertEqual((v.returncode, reason(v)), (2, 'release_signature_invalid'))

    def test_one_changed_byte_in_the_sums_is_refused(self):
        p = self.r.dir / 'SHA256SUMS'
        b = bytearray(p.read_bytes())
        b[0] = ord('f') if b[0] != ord('f') else ord('e')
        p.write_bytes(bytes(b))
        v = self.r.verify()
        self.assertEqual((v.returncode, reason(v)), (2, 'release_signature_invalid'))

    def test_one_changed_byte_in_an_archive_for_this_platform_is_a_digest_mismatch(self):
        p = self.r.dir / 'cortex-core-linux-arm64.oci.tar'
        p.write_bytes(p.read_bytes() + b'x')
        v = self.r.verify()
        self.assertEqual((v.returncode, reason(v)), (2, 'artifact_digest_mismatch:cortex-core-linux-arm64.oci.tar'))

    def test_an_archive_of_the_other_platform_is_not_needed(self):
        (self.r.dir / 'cortex-api-linux-amd64.oci.tar').unlink()
        v = self.r.verify()
        self.assertEqual((v.returncode, v.stderr.strip()), (0, ''), v.stderr)

    def test_a_missing_archive_for_this_platform_is_refused(self):
        (self.r.dir / 'cortex-api-linux-arm64.oci.tar').unlink()
        v = self.r.verify()
        self.assertEqual((v.returncode, reason(v)), (2, 'artifact_missing:cortex-api-linux-arm64.oci.tar'))

    def test_a_missing_signature_is_refused(self):
        (self.r.dir / 'SHA256SUMS.minisig').unlink()
        v = self.r.verify()
        self.assertEqual((v.returncode, reason(v)), (2, 'release_signature_missing'))

    def test_a_platform_the_release_does_not_carry_is_refused(self):
        v = self.r.verify(arch='riscv64')
        self.assertEqual((v.returncode, reason(v)), (2, 'platform_not_in_release'))

    def test_release_json_must_be_a_line_of_the_signed_sums(self):
        p = self.r.dir / 'release.json'
        p.write_bytes(p.read_bytes() + b' ')
        v = self.r.verify()
        self.assertEqual((v.returncode, reason(v)), (2, 'artifact_digest_mismatch:release.json'))


class InstallSpine(unittest.TestCase):
    """install.sh runs verify before any effect. A stub podman records calls; PATH has only the stub, sh tools and minisign."""

    def setUp(self):
        self.r = Release()
        self.addCleanup(self.r.cleanup)
        self.assertEqual(self.r.written.returncode, 0, 'the writer must produce the signed release these tests install')
        self.bin = self.r.tmp / 'bin'
        self.bin.mkdir()
        (self.bin / 'podman').write_text(STUB)
        (self.bin / 'podman').chmod(0o755)
        self.log = self.r.tmp / 'stub.log'
        self.prefix = self.r.tmp / 'prefix'
        self.vault = self.r.tmp / 'vault'

    def run_install(self, *extra, version=None, path=None, pub=None, pinned=False):
        env = dict(os.environ, PATH=path or f'{self.bin}:{os.environ["PATH"]}', STUB_LOG=str(self.log))
        if version:
            env['STUB_VERSION'] = version
        return sh(['sh', str(self.r.dir / 'install.sh'), '--vault', str(self.vault), '--project', 'demo', '--prefix', str(self.prefix),
                   *([] if pinned else ['--trust-key-file', str(pub or self.r.pub)]), '--platform', 'linux-arm64', *extra], env=env)

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def effects(self):
        return [c for c in self.calls() if not c.startswith('--version')]

    def test_refuse_before_effects_for_every_bad_release(self):
        cases = {}
        cases['wrong_key'] = (dict(pub=self.r.pub2), 'release_signature_invalid')
        for name, mutate, want in (
                ('tampered_sums', lambda: (self.r.dir / 'SHA256SUMS').write_bytes((self.r.dir / 'SHA256SUMS').read_bytes()[:-1] + b'0'), 'release_signature_invalid'),
                ('tampered_archive', lambda: (self.r.dir / 'cortex-api-linux-arm64.oci.tar').write_bytes(b'changed'), 'artifact_digest_mismatch:cortex-api-linux-arm64.oci.tar'),
                ('no_signature', lambda: (self.r.dir / 'SHA256SUMS.minisig').unlink(), 'release_signature_missing')):
            fresh = Release()
            self.addCleanup(fresh.cleanup)
            self.r = fresh
            self.setUp_after(fresh)
            mutate()
            res = self.run_install()
            self.assertEqual((res.returncode, reason(res)), (2, want), name)
            self.assertEqual(self.effects(), [], name)
            self.assertFalse(self.prefix.exists(), name)
        fresh = Release()
        self.addCleanup(fresh.cleanup)
        self.r = fresh
        self.setUp_after(fresh)
        res = self.run_install(pub=fresh.pub2)
        self.assertEqual((res.returncode, reason(res)), (2, 'release_signature_invalid'))
        self.assertEqual(self.effects(), [])

    def setUp_after(self, fresh):
        self.bin = fresh.tmp / 'bin'
        self.bin.mkdir()
        (self.bin / 'podman').write_text(STUB)
        (self.bin / 'podman').chmod(0o755)
        self.log = fresh.tmp / 'stub.log'
        self.prefix = fresh.tmp / 'prefix'
        self.vault = fresh.tmp / 'vault'

    def test_without_a_trust_key_file_the_pinned_release_key_decides_and_a_foreign_signature_is_refused(self):
        res = self.run_install(pinned=True)
        self.assertEqual((res.returncode, reason(res)), (2, 'release_signature_invalid'))
        self.assertEqual(self.effects(), [])
        self.assertFalse(self.prefix.exists())

    def test_a_missing_minisign_is_a_typed_refusal_with_no_effects(self):
        res = self.run_install(path=f'{self.bin}:/usr/bin:/bin')
        self.assertEqual((res.returncode, reason(res)), (2, 'missing_tool:minisign'))
        self.assertEqual(self.effects(), [])

    def test_an_old_podman_is_refused_with_no_effects(self):
        res = self.run_install(version='5.9.0')
        self.assertEqual((res.returncode, reason(res)), (2, 'podman_too_old:5.9.0'))
        self.assertEqual(self.effects(), [])

    def test_a_verified_release_still_refuses_where_later_phases_are_not_wired_and_runs_no_effect(self):
        res = self.run_install()
        self.assertEqual((res.returncode, reason(res)), (2, 'install_not_wired:make_secret'))
        self.assertEqual(self.effects(), [])
        self.assertFalse(self.prefix.exists())

    def test_load_phase_loads_only_this_platforms_archives_and_records_only_what_it_made(self):
        res = self.run_install('--only', 'load')
        self.assertEqual(res.returncode, 0, res.stderr)
        loads = [c for c in self.effects() if c.startswith('load')]
        self.assertEqual(sorted(l.split()[-1].rsplit('/', 1)[1] for l in loads), sorted(ARCHIVES['arm64']))
        record = json.loads((self.prefix / 'install-record.json').read_text())
        self.assertEqual(record['release_id'], IDENTITY['release_id'])
        names = sorted(o['name'] for o in record['objects'])
        self.assertEqual(names, [f'localhost/cortex-v2-api:{IDENTITY["release_id"]}', f'localhost/cortex-v2-core:{IDENTITY["release_id"]}'])
        self.assertTrue(all(o['kind'] == 'image' and o['id'].startswith('sha256:') for o in record['objects']))
        other = [c for c in self.effects() if 'amd64' in c]
        self.assertEqual(other, [])

    def test_load_phase_rerun_changes_nothing_in_the_record(self):
        self.run_install('--only', 'load')
        first = (self.prefix / 'install-record.json').read_text()
        again = self.run_install('--only', 'load')
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual((self.prefix / 'install-record.json').read_text(), first)

    def test_install_script_is_one_posix_file_of_at_most_four_hundred_lines(self):
        self.assertTrue(INSTALL_SH.is_file(), 'next/release/ship/install/install.sh')
        self.assertLessEqual(len(INSTALL_SH.read_text().splitlines()), 400)
        self.assertEqual(sh(['sh', '-n', str(INSTALL_SH)]).returncode, 0)


class SelectedHelperAuthority(InstallSpine):
    """HALE-A2-NIX-SIGN-001: the release folder is untrusted until the signature is authenticated, so NO file selected from it may execute first. The stock installer (the
    already trusted bootstrap) is the one verification authority; an inert marker write proves execution, a stub podman proves effects. Inert only: no real engine, key or install."""
    EVIL = '#!/bin/sh\nprintf x >> "$MARKER"\nverify_release() { return 0; }\n'

    def run_stock(self, release_dir, *extra, pub=None, pinned=False):
        marker = self.r.tmp / 'marker'
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', STUB_LOG=str(self.log), MARKER=str(marker))
        res = sh(['sh', str(INSTALL_SH), '--release-dir', str(release_dir), '--vault', str(self.vault), '--project', 'demo', '--prefix', str(self.prefix),
                  *([] if pinned else ['--trust-key-file', str(pub or self.r.pub)]), '--platform', 'linux-arm64', *extra], env=env)
        return res, marker

    def test_a_substituted_release_helper_never_runs_and_an_unsigned_release_is_still_refused_with_no_effects(self):
        (self.r.dir / 'SHA256SUMS.minisig').unlink()
        (self.r.dir / 'verify.sh').write_text(self.EVIL)
        res, marker = self.run_stock(self.r.dir)
        self.assertFalse(marker.exists(), 'release-selected code executed before authentication')
        self.assertEqual((res.returncode, reason(res)), (2, 'release_signature_missing'))
        self.assertEqual(self.effects(), [])
        self.assertFalse(self.prefix.exists())

    def test_a_substituted_helper_beside_a_correctly_signed_release_is_never_executed_either(self):
        (self.r.dir / 'verify.sh').write_text(self.EVIL)
        res, marker = self.run_stock(self.r.dir, '--only', 'load')
        self.assertFalse(marker.exists())
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_a_forged_helper_cannot_turn_a_wrongly_signed_release_into_an_install(self):
        (self.r.dir / 'verify.sh').write_text(self.EVIL)
        res, marker = self.run_stock(self.r.dir, pub=self.r.pub2)
        self.assertFalse(marker.exists())
        self.assertEqual((res.returncode, reason(res)), (2, 'release_signature_invalid'))
        self.assertEqual(self.effects(), [])

    def test_the_trusted_valid_release_still_installs_without_any_helper_file_in_it(self):
        (self.r.dir / 'verify.sh').unlink(missing_ok=True)
        res, marker = self.run_stock(self.r.dir, '--only', 'load')
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertFalse(marker.exists())
        self.assertEqual(len([c for c in self.effects() if c.startswith('load')]), 2)


if __name__ == '__main__':
    unittest.main()
