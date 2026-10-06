"""CM2 signed runtime objects: fixed read-only projections, native engine intercepted."""
import copy
import json
import time

import pytest

from test_cm2_linux_installation_binding import fixture, VERSION

CASES = ['valid', 'container-name', 'container-id', 'container-owner', 'container-class',
         'container-image', 'container-running', 'container-extra', 'extra-network',
         'api-public-bind', 'api-port', 'other-publish', 'network-name', 'network-id',
         'network-owner', 'network-internal', 'network-internal-int', 'image-id',
         'image-digest', 'image-source', 'image-version', 'image-class', 'image-os',
         'image-arch', 'context-change', 'command-refused', 'command-exception', 'malformed',
         'deadline', 'record-replaced', 'helper-replaced', 'signature-refused']


@pytest.mark.parametrize('case', CASES)
def test_exact_owned_runtime_reads_bind_all_signed_images_and_loopback_network(case, tmp_path, monkeypatch):
    module, custody, root, package, release, manifest, record, record_path, signatures, engine_calls = fixture(tmp_path, monkeypatch)
    assert hasattr(module, 'read_linux_runtime'), 'concrete signed runtime object binding is absent'
    roles = list(custody.ROLES); network = record['namespace'] + '_net'; calls = []
    context = {'executable': '/usr/bin/podman', 'environment': {'PATH': '/usr/bin:/bin'}, 'identity': ('public-fixture',)}
    context_reads = []
    def local_context():
        context_reads.append(True)
        return {**context, 'identity': ('changed',)} if case == 'context-change' and len(context_reads) > 1 else context
    monkeypatch.setattr(module, '_local_engine_context', local_context)
    def capture(command, *, environment, deadline):
        assert command[:2] == ['/usr/bin/podman', '--remote=false'] and len(command) == 7
        assert command[3:5] == ['inspect', '--format'] and environment == context['environment']
        template, target = command[5:]; kind = command[2]
        assert kind in ('container', 'image', 'network') and 'json' in template
        assert not any(s in template for s in ('.Env', '.Secrets', '.CreateCommand', '.Cmd', 'json .Config}'))
        assert signatures == ['signature'] and engine_calls == [True]
        assert 0 < deadline - time.monotonic() <= 5
        calls.append((kind, target))
        if len(calls) == 1:
            if case == 'command-refused': raise custody.PrerequisiteRefusal('cortex_podman_unsupported')
            if case == 'command-exception': raise RuntimeError('PUBLIC_UNKNOWN_OBJECT_TEST_ERROR')
            if case == 'malformed': return []
            if case == 'deadline': time.sleep(.08)
            if case in ('record-replaced', 'helper-replaced'):
                selected = record_path if case == 'record-replaced' else package / 'bin/cortex'
                replacement = root / 'public-replacement'; replacement.write_bytes(selected.read_bytes())
                replacement.chmod(selected.stat().st_mode & 0o777); replacement.replace(selected)
        if kind == 'container':
            role = next(r for r in roles if target == record['namespace'] + '_' + r)
            assert '.Config.Labels' in template and '.State.Running' in template
            assert '.NetworkSettings.Networks' in template and '.HostConfig.PortBindings' in template
            value = {'name': target, 'id': format(roles.index(role) + 1, '064x'),
                     'image_id': release['images'][role]['config_id'].removeprefix('sha256:'),
                     'owner': record['installation'], 'deployment_class': 'TEST', 'running': True,
                     'networks': {network: {'NetworkID': network}},
                     'ports': {'8601/tcp': [{'HostIp': '127.0.0.1', 'HostPort': str(record['port'])}]} if role == 'api' else None}
            if role == roles[0]:
                field = {'container-name': 'name', 'container-id': 'id', 'container-owner': 'owner',
                         'container-class': 'deployment_class', 'container-image': 'image_id',
                         'container-running': 'running'}.get(case)
                if field: value[field] = False if field == 'running' else 'foreign'
                if case == 'container-extra': value['foreign'] = True
                if case == 'extra-network': value['networks']['foreign'] = {}
                if case == 'other-publish': value['ports'] = {'5432/tcp': [{'HostIp': '127.0.0.1', 'HostPort': '15499'}]}
            if role == 'api':
                if case == 'api-public-bind': value['ports']['8601/tcp'][0]['HostIp'] = '0.0.0.0'
                if case == 'api-port': value['ports']['8601/tcp'][0]['HostPort'] = '18603'
        elif kind == 'image':
            role = next(r for r in roles if target == release['images'][r]['config_id'])
            assert '.Digest' in template and '.Architecture' in template and '.Os' in template and '.Labels' in template
            value = {'id': target.removeprefix('sha256:'), 'digest': release['images'][role]['manifest_digest'],
                     'os': 'linux', 'architecture': 'amd64', 'source': release['source_revision'],
                     'version': VERSION, 'deployment_class': 'TEST'}
            if role == roles[0]:
                field = {'image-id': 'id', 'image-digest': 'digest', 'image-source': 'source',
                         'image-version': 'version', 'image-class': 'deployment_class',
                         'image-os': 'os', 'image-arch': 'architecture'}.get(case)
                if field: value[field] = 'foreign'
        else:
            assert target == network and '.Internal' in template and '.Labels' in template
            value = {'name': network, 'id': 'a' * 64, 'owner': record['installation'], 'internal': True}
            field = {'network-name': 'name', 'network-id': 'id', 'network-owner': 'owner'}.get(case)
            if field: value[field] = 'foreign'
            if case == 'network-internal': value['internal'] = False
            if case == 'network-internal-int': value['internal'] = 1
        return copy.deepcopy(value)
    monkeypatch.setattr(module, '_engine_json', capture)
    if case == 'signature-refused':
        def refused(*args, **kwargs): raise custody.PrerequisiteRefusal('cortex_release_signature_invalid')
        monkeypatch.setattr(custody, 'verify_signature', refused)
    start = time.monotonic(); deadline = start + (.05 if case == 'deadline' else 5)
    if case == 'valid':
        value = module.read_linux_runtime(root, kos_policy={'minimum_version': '6.0.2'}, deadline=deadline)
        assert value['installation_id'] == record['installation'] and value['origin'] == 'http://127.0.0.1:18602'
        assert value['containers'] == {r: format(i + 1, '064x') for i, r in enumerate(roles)}
        assert value['network_id'] == 'a' * 64 and value['release'] == release
        assert calls == [(kind, record['namespace'] + '_' + r if kind == 'container' else release['images'][r]['config_id']) for r in roles for kind in ('container', 'image')] + [('network', network)]
    else:
        with pytest.raises(custody.PrerequisiteRefusal) as error:
            module.read_linux_runtime(root, kos_policy={'minimum_version': '6.0.2'}, deadline=deadline)
        assert 'PUBLIC_UNKNOWN_OBJECT_TEST_ERROR' not in str(error.value)
        assert len(calls) <= 11
        if case in ('context-change', 'signature-refused'): assert not calls
        if case in ('command-refused', 'command-exception', 'malformed', 'deadline', 'record-replaced', 'helper-replaced'): assert len(calls) == 1
    assert time.monotonic() - start < 1
    assert all(kind in ('container', 'image', 'network') for kind, target in calls)
