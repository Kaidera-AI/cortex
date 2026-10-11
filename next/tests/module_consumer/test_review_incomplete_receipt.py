"""Proposed native C07 RED control; NOT_RUN by Mike (memory admission <35%).

Run with the original C07 copied-PG fixture on a separately admitted stack.
Keep original fixture/product bytes; add this test directory to PYTHONPATH with
next/tests/module_consumer so its common module can be imported.
"""
from common import ConsumerFixture


class IncompleteReceipt(ConsumerFixture):
    def test_event_id_only_ack_cannot_advance_completeness_without_target_effect(self):
        self.seed()
        port = self.port()

        class NoEffectSink:
            def apply(self, event, generation):
                return {'event_id': event.envelope['event_id']}

        port.sink = NoEffectSink()
        port.register(snapshot_cursor=0)
        with self.assertRaises(self.module().ConsumerError):
            port.cycle()
        self.assertIsNone(self.target())
        self.assertEqual(self.checkpoint()[1], 0)
        self.assertEqual(self.outcomes(), [])
