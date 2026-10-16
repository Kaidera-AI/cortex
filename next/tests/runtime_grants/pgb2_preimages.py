"""PG-B2 fixture admitted to Finn's existing private P2a history contract.

SOURCE DATA ONLY. No PostgreSQL, finite-role adapter, recovery authority or
installed-owner qualification is supplied here. Cox can reuse this fixture's
history/context with the existing snapshot/dispatch/measure private adapter,
then assert_restored(adapter.snapshot()). Original PG-O181 stays separate.
All original bytes come from the frozen literal, never the changed live state.
"""
import hashlib
import json
from pathlib import Path

from cortex_core.release.codec import canonical_bytes
from cortex_core.release.contracts import freeze, thaw
import test_lifecycle_receipt as storage

FIXTURE = json.loads((Path(__file__).parent/'fixtures/pgb2-preimages.json').read_bytes())
FIELDS = ('role', 'acl', 'database', 'hba', 'carrier')


class PGPreimageFixture:
    """Test-owned P1 history and source-DATA proposal; caller owns scratch root."""

    def __init__(self, check, root, *, reserve=True):
        self.check = check
        self.helper = storage.LifecycleReceiptOracle()
        self.api = self.helper.api()
        self.history = self.api._History(Path(root)/'history')
        self.owner_helper, self.data, self.proposal = self.helper.setup_data()
        self.originals = {k: FIXTURE['originals_utf8'][k].encode() for k in FIELDS}
        check.assertEqual(set(self.originals), set(self.api._PREIMAGES))
        check.assertEqual({k: hashlib.sha256(v).hexdigest() for k, v in self.originals.items()},
                          FIXTURE['original_sha256'])
        self.identity = thaw(self.proposal.context)['identity']
        if reserve:
            self.reserved = self.api._reserve(self.history, self.data['request_raw'].encode(),
                self.proposal, {k: v.encode() for k, v in self.data['originals'].items()},
                self.originals, clock_raw=self.data['current_clock_raw'].encode())
            self.preimages_ref = self.api._retained_preimages(self.history, self.identity)

    def reload(self):
        self.history = self.api._History(self.history.root)
        return self

    def retained_originals(self, identity=None):
        """Use the real operation-bound lookup and measured RecordRef reads."""
        ref = self.api._retained_preimages(self.history, identity or self.identity)
        bundle = json.loads(self.history.read(ref))
        self.check.assertEqual(set(bundle), {'identity', 'originals'})
        self.check.assertEqual(set(bundle['originals']), set(FIELDS))
        return {k: self.history.read(bundle['originals'][k]) for k in FIELDS}

    def consume(self):
        self.current, self.clock = self.helper.current(self.owner_helper, self.data,
            self.proposal.observed_state_revision+1)
        self.consumed = self.api._consume(self.history, self.reserved, self.current,
            self.clock, originals=self.helper.current_originals)
        # Match _execute_bound's post-consume context, not a caller-selected root.
        dispatch, _ = self.helper.current(self.owner_helper, self.data,
            self.proposal.observed_state_revision+2)
        self.context = freeze({'context': thaw(dispatch.context),
                              'journal_ref': self.consumed,
                              'preimages_ref': self.preimages_ref})
        return self.context

    def assert_restored(self, measured):
        """Independent byte oracle usable with the candidate's snapshot()."""
        self.check.assertEqual(set(measured), set(FIELDS), 'PG-B2 snapshot fields')
        for field in FIELDS:
            self.check.assertIs(type(measured[field]), bytes)
            self.check.assertEqual(measured[field], self.originals[field],
                                   'PG-B2 original bytes: '+field)


class ByteRecorder:
    """Explicit test adapter for fixture admission ONLY, not Cox implementation.

    It writes five owned scratch files. It reads every retained original before
    any write; measure rereads the files independently. No SQL or finite-port
    behavior is simulated or credited. No new production adapter is exported.
    """

    def __init__(self, fixture, root):
        self.fixture = fixture
        self.root = Path(root)/'byte-recorder'
        self.root.mkdir()
        self.dispatches = 0
        for k in FIELDS:
            (self.root/k).write_bytes(fixture.originals[k])

    def snapshot(self):
        return {k: (self.root/k).read_bytes() for k in FIELDS}

    def change(self):
        for k in FIELDS:
            (self.root/k).write_bytes(FIXTURE['changed_utf8'][k].encode())

    def dispatch(self, context):
        f = self.fixture
        f.check.assertEqual(context, f.context)
        _, obs = f.api._load_entry(f.history, context['journal_ref'])
        attempt = json.loads(f.history.read(obs['attempt_ref']))
        f.check.assertEqual(attempt['dispatch_state'], 'consumed_result_unknown')
        f.check.assertEqual(obs['preimages_ref'], context['preimages_ref'])
        values = f.retained_originals(thaw(context['context'])['identity'])
        self.dispatches += 1
        for k, raw in values.items():
            (self.root/k).write_bytes(raw)

    def measure(self, context):
        c = thaw(context['context'])
        # Observed refs describe independently reread test files; this is a DATA
        # subject in P1's existing open observed object, not a new wire schema.
        refs = {k: self.fixture.history.retain(raw) for k, raw in self.snapshot().items()}
        return canonical_bytes({'identity': c['identity'], 'effect': c['effect'],
            'request_ref': c['request_ref'], 'state': 'applied_verified',
            'observed': {'originals': refs}})
