"""R221 Linux producer operation/effect contract; no engine, keychain or real credential."""
import copy
import importlib
import json
import secrets
from contextlib import contextmanager
from pathlib import Path

import pytest

HOME = Path(__file__).parent / 'fixtures/cm2-revision4'
EXECUTION = json.loads((Path(__file__).parent / 'fixtures/cm2-linux-execution-map.json').read_text())
CASES = [case for case in json.loads((HOME / 'fixtures/cases.json').read_text())['cases'] if case['id'] in EXECUTION['current_producer']]

def product():
    path = Path(__file__).parents[1] / 'src/cortex_v2/clients/provisioning.py'
    assert path.is_file(), 'accepted CM2 finite producer is absent'
    return importlib.import_module('cortex_v2.clients.provisioning')

class PrivatePorts:
    """Explicit intercepted native boundaries, independent of expected outcome."""
    def __init__(self, module, case, root):
        self.module, self.setup, self.root = module, case['setup'], root
        self.actions, self.published, self.key_writes = [], [], []
        self.owner = secrets.token_urlsafe(32).encode()
        self.lead, self.console = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self.tokens = (self.owner.decode(), self.lead, self.console)
        self.old = root / 'old-runtime'
        self.old.mkdir(mode=0o700)
        (self.old / 'unit').write_bytes(b'old fixture unit')
        self.response = {
            'operation_id': '60000000-0000-4000-8000-000000000006',
            'project_id': '40000000-0000-4000-8000-000000000004',
            'project_key': 'kos-primary', 'delivery_state': 'issued_once',
            'lead': {'principal_id': '70000000-0000-4000-8000-000000000007', 'manager': 'kos', 'expires_at': '2027-10-06T05:00:00Z'},
            'console': {'principal_id': '20000000-0000-4000-8000-000000000002', 'manager': 'kos', 'expires_at': '2027-10-06T05:00:00Z'},
            'lead_token': self.lead, 'console_token': self.console,
        }
        self.complete = self.setup.get('both_key_writes') == 'already_verified' or self.setup.get('mode') == 'adopt-enrolled'
        if self.setup.get('private_key') == 'absent':
            self.complete = False
        self.roster_reads = 0

    def validate_installation(self, request):
        self.actions.append('verify-signed-native-installation')
        return {'installation_id': '10000000-0000-4000-8000-000000000001', 'host_os': request['host_os']}

    @contextmanager
    def operation_lock(self, request):
        self.actions.append('lock')
        yield self.setup.get('local_operation_lock') != 'held'

    def owner_authorized(self, token, context):
        self.actions.append('owner-probe')
        assert token == self.owner
        return self.setup.get('caller') != 'member'

    def private_command(self, mode, body, *, owner, idempotency_key, operation_id=None):
        self.actions.append(('private-command', mode, copy.deepcopy(body), idempotency_key, operation_id))
        assert owner == self.owner
        if self.setup.get('project') == 'existing_unselected':
            raise self.module.ProvisionRefusal('cortex_provisioning_conflict')
        if self.setup.get('transport_result') == 'uncertain':
            raise TimeoutError('intercepted once-only response uncertainty')
        value = copy.deepcopy(self.response)
        if self.setup.get('response') == 'receipt_only' or self.setup.get('replay_receipt_only'):
            value.pop('lead_token'); value.pop('console_token')
            value['delivery_state'] = 'reissue_required'
        return value

    def store_recipients(self, response, request):
        self.key_writes.append('lead')
        assert response['lead_token'] == self.lead
        if self.setup.get('console_store') == 'failed':
            raise OSError('intercepted private store failure')
        self.key_writes.append('console')
        assert response['console_token'] == self.console
        self.complete = True

    def keys_complete(self, response, request):
        self.actions.append('verify-both-private-recipients')
        return self.complete

    def member_snapshot(self, request):
        self.roster_reads += 1
        self.actions.append('member-profile-and-roster')
        if self.setup.get('roster_member') == 'absent':
            return None
        revision = self.setup.get('initial_roster_revision', self.setup.get('initial_and_final_roster_revision', 0))
        if self.roster_reads > 1:
            revision = self.setup.get('before_publication_roster_revision', revision)
        return {'roster_revision': revision, 'principal_id': self.response['console']['principal_id'], 'actor_id': '30000000-0000-4000-8000-000000000003', 'scope_id': self.response['project_id'], 'project': 'kos-primary', 'member_name': 'console', 'actor_kind': 'service', 'role': 'member', 'status': 'active', 'can_read': True, 'can_write': True, 'can_publish': False, 'project_root': request['create_project']['repo_root']}

    def native_readiness(self, request, member):
        self.actions.append('fresh-read-only-native-readiness')
        return {'status': 'READY', 'installation_id': '10000000-0000-4000-8000-000000000001'}

    def publish_prerequisite(self, request, member, proof):
        self.actions.append('publish-connection-then-descriptor')
        self.published.extend(('connection', 'descriptor'))
        return {'connection_file': str(self.root / 'connection.json'), 'descriptor_file': str(self.root / 'prerequisite.json')}

    def warning(self, message):
        self.actions.append(('warning', message))

    def prepare_mac(self, request):
        self.actions.append('private-verified-stage-and-two-callers')
        if self.setup.get('crash_point'):
            raise OSError('intercepted preparation crash')
        return {'status': 'PREPARED', 'verifier_qualified': True, 'console_qualified': True}

@pytest.mark.parametrize('case', CASES, ids=lambda c: c['id'])
def test_all_frozen_producer_operations_and_effects(case, tmp_path):
    module = product()
    io = PrivatePorts(module, case, tmp_path)
    request = json.loads((HOME / 'fixtures/provisioning.json').read_text())['modes']['create-project']['request']
    request['repo_root'] = str(tmp_path / 'project')
    request['roots'] = [{'path': request['repo_root'], 'kind': 'primary'}]
    Path(request['repo_root']).mkdir(mode=0o700)
    mode = case['setup'].get('mode', 'adopt-enrolled' if case['id'] == 'enroll-no-token-replay' else 'create-project')
    args = {'mode': mode, 'host_os': 'macos' if mode == 'prepare-mac-access' else 'linux', 'create_project': request, 'idempotency_key': 'fixture-fresh-key', 'operation_id': io.response['operation_id'], 'operator_explicit': case['setup'].get('operator_explicit', True), 'manager_explicit': True}
    try:
        value = module.run_provision(args, owner_token=io.owner, ports=io)
        observed = value['status']
        serialized = json.dumps(value)
    except module.ProvisionRefusal as exc:
        observed, serialized = exc.code, str(exc)
    assert observed == case['expected']
    assert all(token not in serialized for token in io.tokens)
    assert (io.old / 'unit').read_bytes() == b'old fixture unit'
    assert not any(a in ('roster-write', 'enroll', 'legacy-fallback', 'start-engine', 'publish-kos') for a in io.actions if isinstance(a, str))
    private = [a for a in io.actions if isinstance(a, tuple) and a[0] == 'private-command']
    if mode in ('adopt-enrolled', 'prepare-mac-access'):
        assert private == []
    if observed not in ('READY',):
        assert io.published == []
    if case['id'] == 'reissue-explicit':
        warnings = [a for a in io.actions if isinstance(a, tuple) and a[0] == 'warning']
        assert len(warnings) == 1 and 'both' in warnings[0][1].lower()
        assert io.actions.index(warnings[0]) < io.actions.index(private[0])
        assert private[0][1] == 'reissue-project-keys' and private[0][-1] == io.response['operation_id']
    elif mode == 'create-project' and private:
        assert private[0][2]['source_project'] is None and private[0][2]['with_console'] is True
        assert len(private) == 1 and private[0][1] == 'create-project'
    if case['id'] == 'create-store-failure':
        assert io.key_writes == ['lead']
    if case['id'] == 'adopt-concurrent-roster-refused':
        assert io.roster_reads >= 2 and io.key_writes == []


def fresh_arguments(tmp_path):
    root = tmp_path / 'project'
    root.mkdir(mode=0o700)
    body = copy.deepcopy(json.loads((HOME / 'fixtures/provisioning.json').read_text())['modes']['create-project']['request'])
    body['repo_root'] = str(root)
    body['roots'] = [{'path': str(root), 'kind': 'primary'}]
    return {'mode': 'create-project', 'host_os': 'linux', 'create_project': body,
            'idempotency_key': 'explicit-stable-key', 'operator_explicit': True,
            'manager_explicit': True}


def fixture_ports(module, tmp_path, case='create-success'):
    vector = next(c for c in CASES if c['id'] == case)
    return PrivatePorts(module, vector, tmp_path)


@pytest.mark.parametrize('case', ['implicit', 'mac', 'adopt', 'reissue', 'prepare', 'unknown',
    'empty-key', 'control-key', 'oversized-key', 'extra-field', 'source-project', 'no-console',
    'integer-console', 'console-lead', 'bad-manager', 'missing-lead', 'relative-root',
    'traversal-root', 'linked-root', 'missing-root', 'two-primary', 'wrong-primary', 'oversized-body'])
def test_invalid_or_deferred_requests_refuse_before_all_ports(case, tmp_path):
    module = product(); args = fresh_arguments(tmp_path); io = fixture_ports(module, tmp_path)
    body = args['create_project']
    if case == 'implicit': args['operator_explicit'] = False
    elif case == 'mac': args['host_os'] = 'macos'
    elif case in ('adopt', 'reissue', 'prepare', 'unknown'):
        args['mode'] = {'adopt': 'adopt-enrolled', 'reissue': 'reissue-project-keys',
                        'prepare': 'prepare-mac-access', 'unknown': 'anything'}[case]
    elif case == 'empty-key': args['idempotency_key'] = ''
    elif case == 'control-key': args['idempotency_key'] = 'stable\nother'
    elif case == 'oversized-key': args['idempotency_key'] = 'x' * 129
    elif case == 'extra-field': args['token'] = 'prohibited'
    elif case == 'source-project': body['source_project'] = 'existing'
    elif case == 'no-console': body['with_console'] = False
    elif case == 'integer-console': body['with_console'] = 1
    elif case == 'console-lead': body['lead_name'] = 'console'
    elif case == 'bad-manager': body['lead_key_manager'] = 'ambient'
    elif case == 'missing-lead': body.pop('lead_responsibility')
    elif case == 'relative-root': body['repo_root'] = 'relative'
    elif case == 'traversal-root': body['repo_root'] += '/../other'
    elif case == 'linked-root':
        alias = tmp_path / 'alias'; alias.symlink_to(body['repo_root']); body['repo_root'] = str(alias)
        body['roots'][0]['path'] = str(alias)
    elif case == 'missing-root': body['repo_root'] += '/missing'; body['roots'][0]['path'] = body['repo_root']
    elif case == 'two-primary': body['roots'].append(copy.deepcopy(body['roots'][0]))
    elif case == 'wrong-primary': body['roots'][0]['path'] = str(tmp_path)
    elif case == 'oversized-body': body['roots'][0]['metadata'] = 'x' * 65536
    with pytest.raises(module.ProvisionRefusal): module.run_provision(args, owner_token=io.owner, ports=io)
    assert io.actions == [] and io.key_writes == [] and io.published == []


@pytest.mark.parametrize('case', ['wrong-project', 'bad-operation', 'missing-lead', 'missing-console',
    'one-token', 'bad-token', 'expired', 'same-principal', 'unexpected-secret'])
def test_private_response_validation_precedes_recipient_writes(case, tmp_path):
    module = product(); args = fresh_arguments(tmp_path); io = fixture_ports(module, tmp_path)
    if case == 'wrong-project': io.response['project_key'] = 'foreign'
    elif case == 'bad-operation': io.response['operation_id'] = 'bad'
    elif case == 'missing-lead': io.response.pop('lead')
    elif case == 'missing-console': io.response.pop('console')
    elif case == 'one-token': io.response.pop('console_token')
    elif case == 'bad-token': io.response['console_token'] = 'invalid\ncredential'
    elif case == 'expired': io.response['lead']['expires_at'] = '2000-01-01T00:00:00Z'
    elif case == 'same-principal': io.response['lead']['principal_id'] = io.response['console']['principal_id']
    elif case == 'unexpected-secret': io.response['owner_token'] = io.owner.decode()
    with pytest.raises(module.ProvisionRefusal) as exc: module.run_provision(args, owner_token=io.owner, ports=io)
    assert not any(token in str(exc.value) for token in io.tokens)
    assert io.key_writes == [] and io.published == []
    assert len([a for a in io.actions if isinstance(a, tuple) and a[0] == 'private-command']) == 1


@pytest.mark.parametrize('field,value', [('actor_kind', 'agent'), ('role', 'lead'), ('status', 'inactive'),
    ('can_read', False), ('can_write', False), ('can_publish', True), ('member_name', 'other'),
    ('project', 'foreign'), ('scope_id', '50000000-0000-4000-8000-000000000005'),
    ('principal_id', '50000000-0000-4000-8000-000000000005'), ('project_root', '/foreign')])
def test_exact_console_binding_required_before_publication(field, value, tmp_path):
    module = product(); args = fresh_arguments(tmp_path); io = fixture_ports(module, tmp_path)
    original = io.member_snapshot
    def altered(request):
        data = original(request); data[field] = value; return data
    io.member_snapshot = altered
    with pytest.raises(module.ProvisionRefusal): module.run_provision(args, owner_token=io.owner, ports=io)
    assert io.published == []


@pytest.mark.parametrize('boundary', ['keys', 'readiness', 'second-roster', 'publication'])
def test_late_failure_never_reports_ready_or_reissues(boundary, tmp_path):
    module = product(); args = fresh_arguments(tmp_path); io = fixture_ports(module, tmp_path)
    if boundary == 'keys': io.keys_complete = lambda response, request: False
    elif boundary == 'readiness': io.native_readiness = lambda request, member: {'status': 'FAILED'}
    elif boundary == 'second-roster':
        original = io.member_snapshot
        def changed(request):
            data = original(request)
            if io.roster_reads > 1: data['roster_revision'] += 1
            return data
        io.member_snapshot = changed
    else:
        def fail(request, member, proof): raise OSError('synthetic publication interruption')
        io.publish_prerequisite = fail
    with pytest.raises(module.ProvisionRefusal): module.run_provision(args, owner_token=io.owner, ports=io)
    private = [a for a in io.actions if isinstance(a, tuple) and a[0] == 'private-command']
    assert len(private) == 1 and private[0][1] == 'create-project'
    assert io.published == [] and (io.old / 'unit').read_bytes() == b'old fixture unit'


def test_success_order_and_public_receipt_are_explicit(tmp_path):
    module = product(); args = fresh_arguments(tmp_path); io = fixture_ports(module, tmp_path)
    result = module.run_provision(args, owner_token=io.owner, ports=io)
    assert result['status'] == 'READY'
    actions = [a[0] if isinstance(a, tuple) else a for a in io.actions]
    assert actions.index('verify-signed-native-installation') < actions.index('owner-probe') < actions.index('private-command')
    assert actions.count('member-profile-and-roster') == 2
    assert actions.index('fresh-read-only-native-readiness') < actions.index('publish-connection-then-descriptor')
    allowed = set(json.loads((HOME / 'fixtures/provisioning.json').read_text())['public_output_allowlist'])
    assert set(result) <= allowed
    assert all(token not in json.dumps(result) for token in io.tokens)


def test_execution_map_accounts_for_all_sixteen_producer_vectors():
    vectors = json.loads((HOME / 'fixtures/cases.json').read_text())['cases']
    all_ids = {c['id'] for c in vectors if c['phase'] == 'producer'}
    current = set(EXECUTION['current_producer']); deferred = set(EXECUTION['next_slice_producer'])
    assert len(current) == 9 and len(deferred) == 7 and not current & deferred
    assert current | deferred == all_ids and EXECUTION['hidden_skips'] == 0
