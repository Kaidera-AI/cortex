"""Project retention must not depend on another project's cursor operation."""
import hashlib
from uuid import UUID

from cortex_core.outbox import Outbox, OutboxError

from test_outbox_retention import Fixture, OWNER_A, uid


class ProjectRetention(Fixture):
    def seed(self, project, cursor, event, aggregate):
        body = ('project-%d-cursor-%d' % (project, cursor)).encode()
        digest = hashlib.sha256(body).hexdigest()
        self.admin.execute('INSERT INTO core.payloads(tenant_id,project_id,id,body,sha256) VALUES(%s,%s,%s,%s,%s)',
                           (uid(2), uid(project), uid(aggregate+1000), body, digest))
        with self.admin.transaction():
            self.admin.execute("INSERT INTO core.records VALUES(%s,%s,%s,'memory',1,false)",
                               (uid(2), uid(project), uid(aggregate)))
            self.admin.execute('INSERT INTO core.record_revisions VALUES(%s,%s,%s,1,%s,false)',
                               (uid(2), uid(project), uid(aggregate), uid(aggregate+1000)))
            self.admin.execute('''INSERT INTO coordination.outbox(event_id,installation_id,tenant_id,project_id,
                aggregate_id,aggregate_kind,aggregate_revision,operation,tombstone,schema_version,
                payload_ref,payload_sha256) VALUES(%s,%s,%s,%s,%s,'memory',1,'upsert',false,1,%s,%s)''',
                (uid(event), uid(1), uid(2), uid(project), uid(aggregate), uid(aggregate+1000), digest))
        self.admin.execute('''INSERT INTO coordination.published_events
            (installation_id,cursor,tenant_id,project_id,event_id,published_at)
            VALUES(%s,%s,%s,%s,%s,clock_timestamp()-interval '8 days')''',
            (uid(1),cursor,uid(2),uid(project),uid(event)))
        self.admin.execute('''INSERT INTO coordination.feed_state(installation_id,last_published_cursor)
            VALUES(%s,%s) ON CONFLICT(installation_id)
            DO UPDATE SET last_published_cursor=GREATEST(coordination.feed_state.last_published_cursor,EXCLUDED.last_published_cursor)''',
            (uid(1),cursor))

    def test_own_expired_cursor_prunes_behind_foreign_quarantine(self):
        self.seed(4,1,101,51)
        self.seed(3,2,102,52)
        self.admin.execute("INSERT INTO coordination.quarantine(tenant_id,project_id,event_id,module_id,error_code) VALUES(%s,%s,%s,'graph','poison')",
                           (uid(2),uid(4),uid(101)))
        port = Outbox(self.request, OWNER_A, UUID(uid(1)), UUID(uid(3)))
        self.assertEqual([event.cursor for event in port.page(after=0).events],[2])
        result = port.prune()
        self.assertEqual(result['removed'],1)
        self.assertEqual(self.admin.execute('SELECT cursor FROM coordination.published_events ORDER BY cursor').fetchall(),[(1,)])
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.quarantine WHERE event_id=%s',(uid(101),)).fetchone()[0],1)
        with self.assertRaises(OutboxError) as error:
            port.page(after=0)
        self.assertEqual(error.exception.code,'expired')
        self.assertEqual(port.page(after=2).events,())
