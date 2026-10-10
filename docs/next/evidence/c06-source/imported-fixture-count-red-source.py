"""Late committed events retain a full delivery window after publication."""
import sys
from pathlib import Path
from uuid import UUID
from cortex_core.outbox import Outbox
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'outbox'))
from core_db_fixture import Fixture,OWNER_A,uid
from test_outbox import OutboxTests


class LatePublication(Fixture):
    fixture_event=OutboxTests.fixture_event
    def test_old_pending_event_gets_seven_days_from_actual_publication(self):
        p=Outbox(self.request,OWNER_A,UUID(uid(1)),UUID(uid(3)))
        with self.admin.transaction():self.fixture_event(self.admin,5000,100,old=True)
        p.prune();self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0],1)
        p.publish();p.prune()
        self.assertEqual(self.admin.execute('SELECT count(*) FROM coordination.outbox').fetchone()[0],1)
        self.assertEqual(p.page().events[0].envelope['event_id'],uid(5000))
