"""Disposable PG18 peer-auth regression; needs frozen image already loaded, no pull/build."""
import json
import os
from pathlib import Path
import subprocess
import time
import unittest
import uuid

DEPLOY = Path(__file__).resolve().parents[1]
IMAGE = '33c3908b67c3240cc141d66fe3977e49eb78cc110d0fca28e85d558f6eaf822a'

class PeerAuth(unittest.TestCase):
    def test_fixed_owner_peer_and_denials(self):
        name = 'kaidera-test-db-peer-' + uuid.uuid4().hex[:12]
        def run(*args):
            return subprocess.run(['podman', *args], text=True, capture_output=True, timeout=90)
        script = '''set -Eeuo pipefail
[ "$(id -u)" = 10001 ]
[ "$(id -un)" = 10001 ]
initdb -D /tmp/peer-data -U postgres --auth-local=peer --auth-host=cert >/tmp/init.log
exec postgres -D /tmp/peer-data -k /var/run/postgresql -h 127.0.0.1 -c hba_file=/etc/cortex/pg_hba.conf -c ident_file=/etc/cortex/pg_ident.conf
'''
        try:
            q = run('run', '-d', '--name', name, '--pull=never', '--platform=linux/amd64',
                    '--user', '10001:10001', '--security-opt', 'no-new-privileges', '--read-only',
                    '--cap-drop=ALL', '--cap-add=CHOWN,DAC_OVERRIDE,FOWNER,SETUID,SETGID',
                    '--network=none', '--tmpfs', '/tmp:rw,noexec,nosuid,size=128m,mode=1777',
                    '--tmpfs', '/var/run/postgresql:rw,noexec,nosuid,size=16m,mode=3775',
                    '-v', str(DEPLOY/'pg_hba.conf')+':/etc/cortex/pg_hba.conf:ro',
                    '-v', str(DEPLOY/'pg_ident.conf')+':/etc/cortex/pg_ident.conf:ro',
                    '--entrypoint', '/bin/bash', IMAGE, '-c', script)
            self.assertEqual(q.returncode, 0, q.stderr)
            for _ in range(80):
                logs = run('logs', name)
                if 'ready to accept connections' in logs.stdout + logs.stderr:
                    break
                state = run('inspect', '--format={{.State.Running}}', name)
                if state.stdout.strip() != 'true':
                    self.fail('PG fixture stopped: '+logs.stdout+logs.stderr)
                time.sleep(.25)
            else:
                self.fail('PG fixture did not start: '+logs.stdout+logs.stderr)
            def psql(user, *opts):
                return run('exec', '--user', user, name, 'psql', '-w', '-U', 'postgres',
                           '-d', 'postgres', '-At', *opts, '-c', 'SELECT current_user')
            owner = psql('10001:10001')
            print('owner10001:', owner.returncode, owner.stdout.strip(), owner.stderr.strip(), flush=True)
            self.assertEqual(owner.returncode, 0, owner.stderr)
            self.assertEqual(owner.stdout.strip(), 'postgres')
            postgres = psql('999:999')
            self.assertEqual(postgres.returncode, 0, postgres.stderr)
            root = psql('0:0')
            self.assertNotEqual(root.returncode, 0)
            self.assertIn('Peer authentication failed', root.stderr)
            tcp = psql('10001:10001', '-h', '127.0.0.1')
            self.assertNotEqual(tcp.returncode, 0)
            self.assertIn('no pg_hba.conf entry', tcp.stderr)
            print('postgres999 allowed; root denied by peer; TCP without certificate denied', flush=True)
        finally:
            cleanup = run('rm', '-f', name)
            if cleanup.returncode:
                raise RuntimeError('Fixture cleanup failed: '+cleanup.stderr)

    def test_network_cert_contract(self):
        hba = [x.strip() for x in (DEPLOY/'pg_hba.conf').read_text().splitlines()
               if x.strip() and not x.lstrip().startswith('#')]
        host = [x for x in hba if x.startswith('host')]
        self.assertEqual(host, ['hostssl platform_agent_memory cortex_app all cert',
                              'hostssl platform_agent_memory postgres all cert map=cortex_admin'])
        self.assertFalse(any('trust' in x.split() for x in hba))
        self.assertIn('cortex_admin cortex-admin postgres', (DEPLOY/'pg_ident.conf').read_text().splitlines())

if __name__ == '__main__':
    unittest.main(verbosity=2)
