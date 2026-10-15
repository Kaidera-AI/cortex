"""Version 2 CONNECT expectations; original 175 test bodies remain unchanged."""
import psycopg
from psycopg import sql
from support import Fixture, ROLE_NAMES


class Connect(Fixture):
    def test_verifier_current_database_capabilities(self):
        self.candidate_if_present()
        for privilege, wanted in (('CONNECT', True), ('TEMP', True), ('CREATE', False),
                                  ('CONNECT WITH GRANT OPTION', False),
                                  ('TEMP WITH GRANT OPTION', False),
                                  ('CREATE WITH GRANT OPTION', False)):
            self.assertIs(self.root.execute(
                'SELECT has_database_privilege(%s,current_database(),%s)',
                (ROLE_NAMES['verifier_owner'], privilege)).fetchone()[0], wanted)

    def test_verifier_size_positive_and_revocation_refuses(self):
        self.candidate_if_present()
        verifier=ROLE_NAMES['verifier_owner']
        database=self.root.execute('SELECT current_database()').fetchone()[0]
        with self.root.transaction():
            self.root.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(verifier)))
            size=self.allowed(self.root, 'SELECT pg_database_size(current_database())')
            self.assertTrue(type(size) is int and size>0, 'SEMANTIC:owned database size unavailable')
        with self.root.transaction():
            self.root.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM {}').format(
                sql.Identifier(database),sql.Identifier(verifier)))
            self.root.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(verifier)))
            self.assertEqual(self.refusal(self.root,'SELECT pg_database_size(current_database())'), '42501')
            self.root.execute('RESET ROLE')
            self.root.execute(sql.SQL('GRANT CONNECT ON DATABASE {} TO {}').format(
                sql.Identifier(database),sql.Identifier(verifier)))
        with self.root.transaction():
            self.root.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(verifier)))
            self.assertGreater(self.allowed(self.root,'SELECT pg_database_size(current_database())'),0)

    def test_verifier_flags_and_all_membership_edges_closed(self):
        self.candidate_if_present()
        verifier=ROLE_NAMES['verifier_owner']
        self.assertEqual(self.root.execute('''SELECT rolcanlogin,rolsuper,rolbypassrls,
            rolcreatedb,rolcreaterole,rolinherit,rolreplication FROM pg_roles WHERE rolname=%s''',
            (verifier,)).fetchone(), (False,)*7)
        self.assertEqual(self.root.execute('''SELECT count(*) FROM pg_auth_members
            WHERE roleid=(SELECT oid FROM pg_roles WHERE rolname=%s)
               OR member=(SELECT oid FROM pg_roles WHERE rolname=%s)''',
            (verifier,verifier)).fetchone()[0],0)

    def test_verifier_real_login_refused(self):
        self.candidate_if_present()
        # Check the deciding role flag independently, so an unrelated connection
        # error cannot stand in for NOLOGIN. The subsequent native probe is real.
        self.assertIs(self.root.execute('SELECT rolcanlogin FROM pg_roles WHERE rolname=%s',
            (ROLE_NAMES['verifier_owner'],)).fetchone()[0],False)
        try:
            self.login('verifier_owner')
        except psycopg.OperationalError as error:
            self.assertIn('not permitted to log in',str(error))
        else:
            self.fail('SEMANTIC:NOLOGIN verifier opened authenticated connection')

    def test_verifier_no_explicit_other_database_grants(self):
        self.candidate_if_present()
        self.assertEqual(self.root.execute('''SELECT count(*) FROM pg_database d
            CROSS JOIN LATERAL aclexplode(d.datacl) a
            WHERE d.datname<>current_database()
              AND a.grantee=(SELECT oid FROM pg_roles WHERE rolname=%s)''',
            (ROLE_NAMES['verifier_owner'],)).fetchone()[0],0)

    def test_verifier_closed_other_database_connect_and_size_refused(self):
        self.candidate_if_present()
        database=self.root.execute('SELECT current_database()').fetchone()[0]
        other=database+'-connect-control'
        self.root.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(other)))
        try:
            self.root.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(other)))
            self.assertIs(self.root.execute("SELECT has_database_privilege(%s,%s,'CONNECT')",
                (ROLE_NAMES['verifier_owner'],other)).fetchone()[0],False)
            with self.root.transaction():
                self.root.execute(sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(ROLE_NAMES['verifier_owner'])))
                self.assertEqual(self.refusal(self.root,'SELECT pg_database_size(%s)',(other,)),'42501')
        finally:
            self.root.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(other)))
