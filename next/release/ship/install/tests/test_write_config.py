"""S2 write_config (day 3): the ONE installer writer of the ONE Core config file `<vault>/<project>/config/cortex.json`.
Source-only proof. The writer builds the closed Core carrier, admits it through Core's own decoder in every mode view BEFORE writing, writes it private/atomic/no-follow, and
re-reads it through Core's own private-file reader. No parser, path or store of our own. Explicit fake boundaries (nothing here is installed or custody proof):
  * no real custody, secret, unlock key or credential exists or is read: the config holds only references (`secrets/cortex/...`), never a value;
  * `owner_uid` is injected where a test cannot chown a real file to a foreign owner (the real owner check is Core's reader plus the writer's own lstat rule);
  * the install.sh caller tests build a throwaway-key signed release in a temp dir at run time (the same fixture as test_sign.py); no key material is committed.
NOT RUN here: the future Core `install` mode view (not in the decoder at this base), real custody, DB, Podman, any native or installed control."""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
INSTALL = HERE.parent
NEXT = HERE.parents[3]
sys.path[:0] = [str(INSTALL), str(HERE), str(NEXT / 'src')]
import config_writer as W  # noqa: E402
import roles as R  # noqa: E402
from cortex_core.release.codec import canonical_bytes  # noqa: E402
from cortex_core.runtime.bootstrap_config import MODE_PURPOSES, read_config_file  # noqa: E402
from cortex_core.runtime.signed_member import Refusal  # noqa: E402
import test_sign as TS  # noqa: E402

VIEWS = ('serve', 'migrate', 'seed')
FIVE = ('bootstrap', 'migrator', 'api_request', 'probe', 'installer_enrollment')
PROJECT, PORT = 'helix', 18601
RENDER = NEXT / 'release/ship/run/render.py'
OPERATIONS = NEXT / 'contracts/runtime/runtime-operations-v1.json'
SOURCE_COMMIT = '7a1078213ac2497d6219e7778c7866001f97f30b'


def tree(root):
    return sorted(str(p.relative_to(root)) for p in Path(root).rglob('*')) if Path(root).exists() else []


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(os.path.realpath(tempfile.mkdtemp(prefix='nix-s2-')))
        self.addCleanup(lambda: subprocess.run(['rm', '-rf', str(self.tmp)]))
        self.vault = self.tmp / 'vault'
        self.vault.mkdir(mode=0o700)
        self.root = self.vault / PROJECT
        self.cfgdir = self.root / 'config'
        self.cfg = self.cfgdir / 'cortex.json'

    def write(self, **kw):
        return W.write_config(str(self.vault), kw.pop('project', PROJECT), kw.pop('port', PORT), **kw)

    def written(self, **kw):
        result = self.write(**kw)
        self.assertEqual(result.get('result'), 'written', result)   # a behavioural predicate, so a missing writer fails here and never as a setup error
        return result

    def refusal(self, **kw):
        with self.assertRaises(Refusal) as cm:
            self.write(**kw)
        return cm.exception.reason

    def views(self):
        return {m: read_config_file(str(self.cfg), mode=m) for m in VIEWS}


class WriterIdentity(Base):
    def test_written_config_is_accepted_by_the_sole_core_decoder_in_every_mode_view_with_one_hash(self):
        result = self.written()
        views = self.views()
        raw = self.cfg.read_bytes()
        doc = json.loads(raw)
        digest = hashlib.sha256(canonical_bytes(doc)).hexdigest()
        self.assertEqual({v.source_sha256 for v in views.values()}, {digest})
        self.assertEqual(result['source_sha256'], digest)
        for mode, view in views.items():
            self.assertEqual(sorted(view.roles), sorted(MODE_PURPOSES[mode]), mode)
        any_view = views['serve']
        self.assertEqual(dict(any_view.project_names), {PROJECT: any_view.project_id})
        self.assertEqual(len({any_view.installation_id, any_view.tenant_id, any_view.project_id}), 3)
        self.assertEqual((any_view.ai_mode, dict(any_view.publish), any_view.database['tls']['mode']), ('off', {'host': '127.0.0.1', 'port': PORT}, 'verify-full'))
        self.assertEqual(any_view.vault_root, str(self.root))

    def test_five_purposes_with_unique_logins_and_references_and_no_secret_value_anywhere(self):
        self.written()
        views = self.views()
        merged = {p: r for v in views.values() for p, r in v.roles.items()}
        self.assertEqual(sorted(merged), sorted(FIVE))
        logins = {p: r.login for p, r in merged.items()}
        self.assertEqual(len(set(logins.values())), 5)
        self.assertEqual(logins['installer_enrollment'], R.INSTALLER_LOGIN)
        R.bind_logins(dict(views['seed'].role_logins), views['seed'].vault_root)   # the sole role-name validator accepts what we wrote
        refs = [r.secret_ref for r in merged.values()] + [views['serve'].client_ref, views['serve'].database['tls']['root_cert_ref']]
        self.assertEqual(len(set(refs)), 7)
        self.assertTrue(all(r.startswith('secrets/cortex/') for r in refs))
        text = self.cfg.read_text().lower()
        for banned in ('password', 'passwd', 'token', 'begin '):   # (test correction: 'private' matched the macOS temp path /private/var, not a key)
            self.assertNotIn(banned, text, banned)

    def test_file_and_directories_are_private_and_owned_by_the_installer(self):
        self.written()
        st = os.lstat(self.cfg)
        self.assertEqual((stat.S_IMODE(st.st_mode), st.st_uid, st.st_nlink, stat.S_ISREG(st.st_mode)), (0o600, os.geteuid(), 1, True))
        for d in (self.root, self.cfgdir):
            s = os.lstat(d)
            self.assertEqual((stat.S_IMODE(s.st_mode) & 0o077, s.st_uid), (0, os.geteuid()), str(d))

    def test_the_existing_renderer_accepts_the_written_config_and_publishes_its_identity(self):
        self.written()
        conf = self.tmp / 'core-conf'
        (conf / 'sql').mkdir(parents=True)
        for n in ('postgresql.conf', 'pg_hba.conf'):
            (conf / n).write_text('# fixture\n')
        for step in ('pre', 'post', 'close'):
            (conf / 'sql' / f'roles-{step}.sql').write_text('-- fixture\n')
        release = self.tmp / 'release.json'
        release.write_text(json.dumps({'source_commit': SOURCE_COMMIT,
                                       'core': {'image': 'sha256:' + 'c' * 64, 'initdb': '/usr/lib/postgresql/bin/initdb',
                                                'postgres': '/usr/lib/postgresql/bin/postgres', 'psql': '/usr/lib/postgresql/bin/psql'},
                                       'api': {'image': 'sha256:' + 'a' * 64}}))
        out = self.tmp / 'render-out'
        proc = subprocess.run([sys.executable, '-I', str(RENDER), '--config', str(self.cfg), '--operations', str(OPERATIONS), '--release', str(release),
                               '--core-conf', str(conf), '--out', str(out)], capture_output=True, text=True, timeout=60)
        self.assertEqual((proc.returncode, proc.stderr), (0, ''))
        inventory = json.loads((out / 'inventory.json').read_text())
        self.assertEqual(inventory['serve']['published'], [{'host': '127.0.0.1', 'port': PORT}])
        installation = self.views()['serve'].installation_id
        self.assertIn(f'ai.kaidera.cortex.installation={installation}', (out / 'bootstrap.plan.json').read_text())

    def test_rerun_with_the_same_inputs_is_a_noop_that_touches_nothing(self):
        self.written()
        before = (self.cfg.read_bytes(), os.lstat(self.cfg).st_ino, os.lstat(self.cfg).st_mtime_ns, tree(self.tmp))
        again = self.write()
        self.assertEqual(again['result'], 'unchanged')
        self.assertEqual((self.cfg.read_bytes(), os.lstat(self.cfg).st_ino, os.lstat(self.cfg).st_mtime_ns, tree(self.tmp)), before)

    def test_rerun_with_different_inputs_is_refused_and_leaves_the_file_unchanged(self):
        self.written()
        before = self.cfg.read_bytes()
        self.assertEqual(self.refusal(port=PORT + 1), 'config_exists_different')
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_only_the_project_directory_the_config_directory_and_the_one_file_are_created(self):
        before = tree(self.tmp)
        self.written()
        self.assertEqual(sorted(set(tree(self.tmp)) - set(before)), sorted([f'vault/{PROJECT}', f'vault/{PROJECT}/config', f'vault/{PROJECT}/config/cortex.json']))


class WriterRefusals(Base):
    def prepare_dirs(self):
        self.root.mkdir(mode=0o700)
        self.cfgdir.mkdir(mode=0o700)

    def test_a_symlink_leaf_is_refused_and_its_target_is_untouched(self):
        self.prepare_dirs()
        decoy = self.tmp / 'decoy'
        decoy.write_text('decoy')
        decoy.chmod(0o600)
        os.symlink(decoy, self.cfg)
        before = tree(self.tmp)
        self.assertEqual(self.refusal(), 'config_path_unsafe')
        self.assertEqual((decoy.read_text(), tree(self.tmp)), ('decoy', before))

    def test_a_symlink_config_directory_is_refused_and_nothing_is_written_through_it(self):
        self.root.mkdir(mode=0o700)
        elsewhere = self.tmp / 'elsewhere'
        elsewhere.mkdir(mode=0o700)
        os.symlink(elsewhere, self.cfgdir)
        self.assertEqual(self.refusal(), 'config_path_unsafe')
        self.assertEqual(tree(elsewhere), [])

    def test_a_symlink_component_of_the_vault_path_is_refused_and_nothing_is_created_under_it(self):
        real = self.tmp / 'real-vault'
        real.mkdir(mode=0o700)
        link = self.tmp / 'link-vault'
        os.symlink(real, link)
        with self.assertRaises(Refusal) as cm:
            W.write_config(str(link), PROJECT, PORT)
        self.assertEqual(cm.exception.reason, 'config_path_unsafe')
        self.assertEqual(tree(real), [])

    def test_a_foreign_owner_is_refused_and_nothing_is_written(self):
        self.prepare_dirs()
        before = tree(self.tmp)
        self.assertEqual(self.refusal(owner_uid=os.geteuid() + 1), 'config_owner')   # fake boundary: expected owner injected, a test cannot chown
        self.assertEqual(tree(self.tmp), before)

    def test_loose_permissions_are_refused_and_never_repaired(self):
        self.prepare_dirs()
        os.chmod(self.cfgdir, 0o777)
        self.assertEqual(self.refusal(), 'config_permissions')
        self.assertEqual(stat.S_IMODE(os.lstat(self.cfgdir).st_mode), 0o777)
        self.assertFalse(self.cfg.exists())
        os.chmod(self.cfgdir, 0o700)
        self.written()
        os.chmod(self.cfg, 0o644)
        self.assertEqual(self.refusal(), 'private_file_required')   # Core's own reason
        self.assertEqual(stat.S_IMODE(os.lstat(self.cfg).st_mode), 0o644)

    def test_a_tls_mismatch_in_an_existing_config_is_refused_by_core_and_left_untouched(self):
        self.written()
        doc = json.loads(self.cfg.read_text())
        doc['database']['tls']['mode'] = 'require'
        self.cfg.write_text(json.dumps(doc))
        self.cfg.chmod(0o600)
        before = self.cfg.read_bytes()
        self.assertEqual(self.refusal(), 'config_transport_unratified')
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_bad_inputs_are_refused_before_any_directory_is_made(self):
        for kw, name in (({'project': 'a/b'}, 'slash'), ({'project': ''}, 'empty'), ({'project': '..'}, 'dots'), ({'project': 'x' * 80}, 'long'),
                         ({'port': 0}, 'zero'), ({'port': 65536}, 'high'), ({'port': True}, 'bool'), ({'port': '80'}, 'text')):
            with self.subTest(name):
                self.assertIn(self.refusal(**kw), ('config_input', 'config_identifier'))
                self.assertEqual(tree(self.vault), [], name)
        for vault, name in (('relative/vault', 'relative'), (str(self.vault) + '/../vault', 'dotdot'), (str(self.tmp / 'missing'), 'missing')):
            with self.subTest(name):
                with self.assertRaises(Refusal):
                    W.write_config(vault, PROJECT, PORT)
        self.assertEqual(tree(self.vault), [])

    def test_a_failed_write_leaves_no_temporary_file(self):
        self.prepare_dirs()
        os.chmod(self.cfgdir, 0o500)
        self.addCleanup(os.chmod, self.cfgdir, 0o700)
        self.assertEqual(self.refusal(), 'config_write_failed')
        os.chmod(self.cfgdir, 0o700)
        self.assertEqual(tree(self.cfgdir), [])


class CallerPath(unittest.TestCase):
    """The real supported caller: install.sh `--only write_config` after the signature check. Throwaway-key signed releases (fake boundary: test keys, not release keys)."""

    def build(self, helper=True, tamper=False):
        data = (INSTALL / 'config_writer.py').read_bytes()
        extra = {'config_writer.py': data + (b'# tampered\n' if tamper else b'')} if helper else {}
        self.r = TS.Release(extra=extra)
        self.addCleanup(self.r.cleanup)
        self.assertEqual(self.r.written.returncode, 0, self.r.written.stderr)
        self.bin = self.r.tmp / 'bin'
        self.bin.mkdir()
        (self.bin / 'podman').write_text(TS.STUB)
        (self.bin / 'podman').chmod(0o755)
        self.log = self.r.tmp / 'stub.log'
        self.vault = self.r.tmp / 'vault'
        self.vault.mkdir(mode=0o700)
        self.prefix = self.r.tmp / 'prefix'

    def install(self, *extra, script=None, port=True, only=True):
        env = dict(os.environ, PATH=f'{self.bin}:{os.environ["PATH"]}', STUB_LOG=str(self.log))
        argv = ['sh', str(script or INSTALL / 'install.sh'), '--release-dir', str(self.r.dir), '--vault', str(self.vault), '--project', PROJECT,
                '--prefix', str(self.prefix), '--trust-key-file', str(self.r.pub), '--platform', 'linux-arm64']
        if port:
            argv += ['--port', str(PORT)]
        if only:
            argv += ['--only', 'write_config']
        return TS.sh([*argv, *extra], env=env)

    def effects(self):
        return [c for c in (self.log.read_text().splitlines() if self.log.exists() else []) if not c.startswith('--version')]

    def test_after_verification_it_writes_exactly_the_one_config_that_core_decodes_and_runs_no_effect(self):
        self.build()
        res = self.install()
        self.assertEqual((res.returncode, TS.reason(res) if res.returncode else ''), (0, ''), res.stderr)
        cfg = self.vault / PROJECT / 'config' / 'cortex.json'
        self.assertEqual({m: read_config_file(str(cfg), mode=m).source_sha256 for m in VIEWS}.keys(), set(VIEWS))
        self.assertEqual(sorted(tree(self.vault)), sorted([PROJECT, f'{PROJECT}/config', f'{PROJECT}/config/cortex.json']))
        self.assertEqual(self.effects(), [])
        self.assertFalse(self.prefix.exists())
        self.assertEqual(self.install().returncode, 0)   # rerun is a no-op success

    def test_a_helper_that_is_not_a_signed_member_never_runs(self):
        self.build(helper=False)
        res = self.install()
        self.assertEqual((res.returncode, TS.reason(res)), (2, 'artifact_missing:config_writer.py'))
        self.assertEqual(tree(self.vault), [])

    def test_a_helper_whose_bytes_differ_from_the_signed_member_never_runs(self):
        self.build(tamper=True)
        res = self.install()
        self.assertEqual((res.returncode, TS.reason(res)), (2, 'artifact_digest_mismatch:config_writer.py'))
        self.assertEqual(tree(self.vault), [])

    def test_an_unsigned_release_writes_nothing(self):
        self.build()
        (self.r.dir / 'SHA256SUMS.minisig').unlink()
        res = self.install()
        self.assertEqual((res.returncode, TS.reason(res)), (2, 'release_signature_missing'))
        self.assertEqual(tree(self.vault), [])

    def test_the_full_run_still_refuses_at_make_secret_before_any_config_is_written(self):
        self.build()
        res = self.install(only=False)
        self.assertEqual((res.returncode, TS.reason(res)), (2, 'install_not_wired:make_secret'))
        self.assertEqual(tree(self.vault), [])
        self.assertEqual(self.effects(), [])

    def test_a_missing_port_is_refused_before_any_effect(self):
        self.build()
        res = self.install(port=False)
        self.assertEqual((res.returncode, TS.reason(res)), (2, 'missing_argument:port'))
        self.assertEqual(tree(self.vault), [])

    def test_a_script_without_the_core_source_beside_it_refuses_typed_and_writes_nothing(self):
        self.build()
        res = self.install(script=self.r.dir / 'install.sh')   # release-folder layout: no next/src two levels up
        self.assertEqual((res.returncode, TS.reason(res)), (2, 'write_config_core_missing'))
        self.assertEqual(tree(self.vault), [])


if __name__ == '__main__':
    unittest.main()
