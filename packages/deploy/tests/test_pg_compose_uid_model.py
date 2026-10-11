"""Source-only identity invariant; native persistent-UID/startup proof is separate."""
from pathlib import Path
import re
import unittest
import yaml
ROOT=Path(__file__).resolve().parents[1]
class PGComposeIdentity(unittest.TestCase):
 def test_certificate_owner_matches_post_bootstrap_uid(self):
  compose=yaml.safe_load((ROOT/'docker-compose.yml').read_text());pg=compose['services']['cortex-pg'];tls=(ROOT/'tls/init.sh').read_text();entry=(ROOT/'db-entrypoint.sh').read_text()
  server=int(re.search(r'server_uid=\$\{CORTEX_TLS_SERVER_UID:-([0-9]+)\}',tls)[1])
  self.assertIn('docker_create_db_directories',entry);self.assertIn('exec gosu postgres "$0" "$@"',entry)
  user=pg.get('user');mapped=int(re.search(r'uid=([0-9]+)',pg['userns_mode'])[1])
  # With explicit root, the committed entrypoint drops to the immutable image's postgres999.
  effective=server if user=='0:0' else mapped
  self.assertEqual(effective,server,'Cortex server certificate missing: tls.crt')
  self.assertEqual(user,'0:0');self.assertEqual(server,999)
  self.assertTrue(pg['read_only']);self.assertEqual(set(pg['cap_add']),{'CHOWN','DAC_OVERRIDE','FOWNER','SETUID','SETGID'})
  self.assertEqual(pg['security_opt'],['no-new-privileges:true']);self.assertNotIn('privileged',pg)
  self.assertIn('cortex-tls-server:/tls/server:ro',pg['volumes']);self.assertIn('cortex-pg-data:/var/lib/postgresql',pg['volumes'])
  self.assertEqual(compose['services']['cortex-tls-init']['user'],'0:0')
  self.assertIn('chmod 0700 "$tls_root/$role"',tls);self.assertIn('install -m 0400 -o "$owner_uid"',tls)
if __name__=='__main__':unittest.main()
