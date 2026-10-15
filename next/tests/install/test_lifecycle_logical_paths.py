"""Reviewer-only DATA witnesses; owned temporary storage, no native/PG effects."""
import json
from pathlib import Path
import tempfile
import unittest

from cortex_core.install import lifecycle as api, owner
from cortex_core.install.contracts import measure_original_ref
import test_lifecycle_receipt as original


class LogicalOriginalPaths(unittest.TestCase):
    def reserve_at(self, path):
        check = original.LifecycleReceiptOracle()
        helper, data, _ = check.setup_data()
        context = json.loads(data['context_raw'])
        old = context['current_state_ref']['path']
        raw = data['originals'].pop(old)
        data['originals'][path] = raw
        helper.context(data, lambda c: c['current_state_ref'].update(path=path))
        # Prove the unchanged existing codec and full B1 source gate accept it.
        context = json.loads(data['context_raw'])
        self.assertEqual(measure_original_ref(context['current_state_ref'], raw.encode()), raw.encode())
        proposal, _ = helper.run_gate(helper.api(), data)
        self.assertFalse(proposal.effect_authority)
        with tempfile.TemporaryDirectory() as root:
            history = api._History(Path(root))
            try:
                entry = api._reserve(history, data['request_raw'].encode(), proposal,
                    {k:v.encode() for k,v in data['originals'].items()}, check.preimages(),
                    clock_raw=data['current_clock_raw'].encode())
            except owner.OwnerRefusal as error:
                self.fail('P2A_VALID_ORIGINAL_PATH_LOST:'+path+':'+error.detail)
            event, observation = api._load_entry(history, entry)
            kept = json.loads(history.read(observation['context_ref']))
            self.assertEqual(history.read(kept['current_state_ref']), raw.encode())
            self.assertEqual(event['phase'], 'create_intent')

    def test_plain_relative_logical_path_keeps_original(self):
        self.reserve_at('records/state.json')

    def test_events_prefix_is_valid_original_not_a_journal_selector(self):
        self.reserve_at('events/state.json')

    def test_journal_shaped_logical_name_keeps_original(self):
        self.reserve_at('events/00000000000000000000.json')
