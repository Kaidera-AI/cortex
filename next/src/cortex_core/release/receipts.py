"""Shared build receipt verifier; no assembly, signing or installation effects."""
from dataclasses import dataclass
from .artifacts import inspect_oci, measure, require
from .codec import canonical_bytes, decode_json, ordered_inventory, schema_validate, sha256
from .contracts import ParsedBuildReceipt, ReceiptRefusal, VerifiedBuildInput, VerifiedBuildReceipt, _SEAL, freeze, thaw

CLASSES = ('accounts', 'cache', 'clock', 'files', 'input', 'labels', 'logs', 'python', 'runtime', 'serialization', 'xattrs-acls')
PHASES = ('input', 'parser', 'build', 'export', 'audit', 'cleanup')
IDENTITIES = ('build_id', 'execution_id', 'store_id', 'runroot_id', 'container_id', 'source_id')


QUALIFICATION_SCHEMA = 'cortex.native-qualification.v1'
QUALIFICATION_KEYS = {'schema', 'architecture', 'platform', 'host', 'engine', 'engine_path', 'engine_config',
                      'inspector', 'probe_image', 'containment', 'cleanup_interpreter', 'cleanup_script'}
PROBES = ('memory', 'mounts_env', 'network', 'pids', 'rootfs_write', 'time', 'tmpfs')
PROBE_REQUEST_SCHEMA = 'cortex.probe-request.v1'
TIME_SETUP_SLACK_SEC = 5  # session root + archive custody copy before the load child


@dataclass(frozen=True)
class MatchedQualification:
    """R01's statement, made only inside the native verifier, that measured R6
    originals matched THIS verification's accepted context (Cox R4-R01-002)."""
    pending: object
    receipt_sha256: str
    input_sha256: str
    closure_sha256: str
    _seal: object


def _hex(value):
    return type(value) is str and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def _ref(value):
    return type(value) is str and 0 < len(value) <= 4096 and not value.startswith('/') and '..' not in value.split('/')


def qualification_complete(doc):
    """The ONE closed R6 record shape. Every row cites ORIGINALS only: its request and
    the real child records of its run (whose streams are originals too); the record
    asserts no outcome. Shape only: R01 re-derives every row (qualification_row)."""
    def row(value):
        return type(value) is dict and set(value) == {'request', 'children'} and all(_ref(v) for v in value.values())
    try:
        valid = (type(doc) is dict and set(doc) == QUALIFICATION_KEYS and doc['schema'] == QUALIFICATION_SCHEMA and
                 doc['architecture'] in ('amd64', 'arm64') and doc['platform'] == 'linux/' + doc['architecture'] and
                 type(doc['host']) is dict and set(doc['host']) == {'os', 'arch', 'uid', 'rootless', 'emulated', 'kernel'} and
                 doc['host']['os'] == 'linux' and doc['host']['rootless'] is True and doc['host']['emulated'] is False and
                 type(doc['host']['uid']) is int and doc['host']['uid'] > 0 and
                 type(doc['engine']) is dict and set(doc['engine']) == {'engine_id', 'version', 'sha256'} and
                 _hex(doc['engine']['sha256']) and type(doc['engine_path']) is str and doc['engine_path'].startswith('/') and
                 type(doc['engine_config']) is dict and set(doc['engine_config']) == {'containers_conf', 'storage_conf'} and
                 all(_hex(v) for v in doc['engine_config'].values()) and
                 type(doc['inspector']) is dict and set(doc['inspector']) == {'closure_sha256', 'positive', 'negative'} and
                 _hex(doc['inspector']['closure_sha256']) and row(doc['inspector']['positive']) and
                 row(doc['inspector']['negative']) and type(doc['probe_image']) is str and
                 doc['probe_image'].startswith('sha256:') and _hex(doc['probe_image'][7:]) and
                 type(doc['containment']) is dict and set(doc['containment']) == set(PROBES) and
                 all(row(doc['containment'][name]) for name in PROBES) and
                 type(doc['cleanup_interpreter']) is str and doc['cleanup_interpreter'].startswith('/') and
                 type(doc['cleanup_script']) is dict and set(doc['cleanup_script']) == {'path', 'sha256'} and
                 type(doc['cleanup_script']['path']) is str and doc['cleanup_script']['path'].startswith('/') and
                 _hex(doc['cleanup_script']['sha256']))
    except (AttributeError, KeyError, TypeError):
        valid = False
    require(valid, 'native_qualification_incomplete')
    return doc


def qualification_paths(doc):
    rows = [doc['inspector']['positive'], doc['inspector']['negative'], *[doc['containment'][n] for n in PROBES]]
    return [value for row in rows for value in (row['request'], row['children'])]


def qualification_streams(children_raw):
    """The stream originals one cited children file references."""
    children = decode_json(children_raw, 8 * 1024 * 1024)
    require(type(children) is list, 'native_qualification_incomplete')
    return [child[key]['path'] for child in children if type(child) is dict
            for key in ('stdout', 'stderr', 'journal') if type(child.get(key)) is dict and _ref(child[key].get('path'))]


def _closed_request(raw, capability, descriptor, profile, input_value):
    """The ONE closed inspector request check (Cox R4-R01-001): the protocol's exact
    keys, the admitted ABI and optimization, sorted unique rows, and the existing
    controller byte checks (artifacts.pyc_row) on every row. Never compiles."""
    import base64
    from .artifacts import pyc_row
    from . import _pyc_check
    fail = 'native_inspector_execution_unqualified'
    limits = profile['inspector']['limits']
    request = decode_json(raw, limits['input_bytes'])
    require(type(request) is dict and set(request) == _pyc_check.REQUEST_KEYS and
            request['schema'] == _pyc_check.REQUEST_SCHEMA and request['closure_sha256'] == capability.closure_sha256 and
            request['policy_sha256'] == capability.policy_sha256 and
            request['abi'] == {'version': descriptor['python']['version'], 'magic': descriptor['python']['magic']} and
            type(request['optimization']) is int and request['optimization'] == profile['inspector']['optimization'] and
            type(request['rows']) is list and bool(request['rows']), fail)
    caches = [row.get('cache') if type(row) is dict else None for row in request['rows']]
    require(caches == sorted(set(caches)) and None not in caches, fail)
    for row in request['rows']:
        require(set(row) == _pyc_check.ROW_KEYS and all(type(row[key]) is str for key in _pyc_check.ROW_KEYS), fail)
        try:
            cache = base64.b64decode(row['cache_b64'], validate=True)
            source = base64.b64decode(row['source_b64'], validate=True)
            checked = pyc_row(row['cache'], cache, {row['source']: source}, input_value)
        except (ValueError, KeyError, TypeError):
            raise ReceiptRefusal(fail) from None
        require(checked['source'] == row['source'] and checked['final_filename'] == row['final_filename'] and
                checked['optimization'] == request['optimization'], fail)
    return request


def _stream(child, key, measured, found=None, fail='native_child_evidence'):
    stream = child.get(key)
    require(type(stream) is dict and stream.get('path') in measured and stream.get('bytes') == len(measured[stream['path']]) and
            stream.get('sha256') == sha256(measured[stream['path']]), fail)
    if found is not None:
        found.add(stream['path'])
    return measured[stream['path']]


def _journal_join(child, measured, found, fail):
    """One child's MEASURED adapter journal (engine.run_operation's two rows) must parse
    and join that child (Vera R6T1-R1): started with the child's exact argv,
    environment names and timeout, inside the child's own interval; then finished/failed
    with the child's exit or error. Returns (started row, closing row)."""
    try:
        rows = [decode_json(line, 1024 * 1024, canonical=False)
                for line in _stream(child, 'journal', measured, found, fail).split(b'\n') if line]
    except ValueError:
        raise ReceiptRefusal(fail) from None
    require(len(rows) == 2 and all(type(r) is dict for r in rows), fail)
    first, last = rows
    started, finished, error = child.get('started_at_unix_ns'), child.get('finished_at_unix_ns'), child.get('error')
    require(set(first) == {'status', 'argv', 'environment_names', 'started_at_unix_ns', 'timeout_ns'} and
            first['status'] == 'started' and first['argv'] == child.get('argv') and
            first['environment_names'] == child.get('environment_names') and
            type(child.get('timeout_ns')) is int and first['timeout_ns'] == child['timeout_ns'] and
            set(last) == {'status', 'exit', 'error', 'finished_at_unix_ns', 'elapsed_ns'} and
            all(type(v) is int for v in (started, finished, first['started_at_unix_ns'], last['finished_at_unix_ns'],
                                         last['elapsed_ns'])) and
            started <= first['started_at_unix_ns'] <= last['finished_at_unix_ns'] <= finished and
            last['elapsed_ns'] >= 0 and last['error'] == error and
            (last['exit'] == child.get('exit') if error is None else child.get('exit') is None) and
            last['status'] == ('finished' if child.get('exit') == 0 and error is None else 'failed'), fail)
    return first, last


def _nested_group(nested, measured, doc, capability, limits, fail, found=None):
    """The six real R3 children of one contained run (load, inspect, run, rm, ps,
    unshare), bound to the engine, the given admitted capability's image and owned
    root, the ONE plan argv, the fixed environment, the session cwd, the controlled
    deleter, full metadata and measured streams. Returns (session, name, run child)."""
    from pathlib import Path
    from .inspector import ENGINE_ENVIRONMENT, plan_argv
    require(type(nested) is list and len(nested) == 6 and all(type(child) is dict for child in nested), fail)
    run = nested[2]
    argv = run.get('argv')
    require(type(argv) is list and len(argv) > 10 and type(argv[3]) is str and argv[3].endswith('/store') and
            '--name' in argv, fail)
    session = argv[3][:-len('/store')]
    name = argv[argv.index('--name') + 1]
    require(str(Path(session).parent) == str(capability.root) and
            tuple(argv) == plan_argv(doc['engine_path'], capability.image_digest, limits, session, name), fail)
    prefix = list(argv[:8])
    expected = [prefix + ['load', '--input', session + '/image.tar'],
                prefix + ['image', 'inspect', '--format', 'json', capability.image_digest], list(argv),
                prefix + ['rm', '--force', '--time', '0', '--ignore', name],
                prefix + ['ps', '-a', '--filter', 'name=^' + name + '$', '--format', '{{.ID}}']]
    require([child.get('argv') for child in nested[:5]] == expected and
            all(child.get('cwd') == session and child.get('environment_names') == sorted(ENGINE_ENVIRONMENT)
                for child in nested), fail)
    _deleter(nested[5]['argv'], prefix, doc, [session + '/store', session + '/run'])
    for child in nested:
        for key in ('stdout', 'stderr'):
            _stream(child, key, measured, found, fail)  # a stopped run keeps its adapter streams too
        _journal_join(child, measured, found, fail)
    require(all(nested[i].get('exit') == 0 for i in (0, 1, 3, 4, 5)) and
            _stream(nested[4], 'stdout', measured, found, fail).strip() == b'', fail)  # the run's container is absent
    return session, name, run


PROBE_OBSERVATION_KEYS = {
    'network': {'probe', 'attempted', 'interfaces', 'errno'},
    'rootfs_write': {'probe', 'target', 'errno', 'written'},
    'tmpfs': {'probe', 'target', 'errno', 'written', 'tmpfs_on_tmp'},
    'pids': {'probe', 'errno', 'pids_max', 'pids_current_at_denial', 'pids_events_max_delta', 'children', 'reaped'},
    'memory': {'probe', 'memory_max', 'attempted_bytes', 'child_signal', 'oom_kill_delta'},
    'mounts_env': {'probe', 'mounts', 'environment'}}


def qualification_row(kind, request_raw, children, measured, context):
    """The ONE frozen outcome table (Hale R6-ORACLE-001 = Vera R6-T2), used by the R6
    producer before any lane call and by R01's match. Child process success is not
    row success; every row also needs its bound cleanup/absence evidence. Returns the
    stream originals it consumed. context = {capability, probe, descriptor, profile,
    input_value, doc, allowlist, positive (request object, for the negative)}."""
    from .inspector import ARCH
    from . import _pyc_check
    fail = 'native_inspector_execution_unqualified'
    profile = context['profile']
    limits = profile['inspector']['limits']
    found = set()
    real = kind in ('positive', 'negative')
    capability = context['capability'] if real else context['probe']
    require(capability is not None, fail)
    _, _, run = _nested_group(children, measured, context['doc'], capability, limits, fail, found)
    if kind == 'time':
        # Stopped by the run deadline, no reply. The MEASURED adapter journal decides
        # (Hale R6-ORACLE-001.TIME): exactly started + failed/TimeoutExpired for THIS
        # argv and environment, its timeout == the collector's, held until that timeout
        # and killed within the fixed kill wait. The window ends at the admitted
        # limits.seconds counted from the run's budget (begun at most
        # TIME_SETUP_SLACK_SEC before the load child); cleanup proves the --name gone.
        from .inspector import KILL_SECONDS
        first, last = _journal_join(run, measured, found, fail)  # already joined in _nested_group; parsed again here
        timeout, started, load = run['timeout_ns'], run['started_at_unix_ns'], children[0].get('started_at_unix_ns')
        require(run.get('error') == 'TimeoutExpired' and last['error'] == 'TimeoutExpired' and type(load) is int and
                timeout > 0 and timeout <= last['elapsed_ns'] <= timeout + int((KILL_SECONDS + 1) * 10 ** 9), fail)
        end = started + timeout + int(KILL_SECONDS * 10 ** 9)
        window = load + limits['seconds'] * 10 ** 9
        require(window - TIME_SETUP_SLACK_SEC * 10 ** 9 <= end <= window, fail)
        require(decode_json(request_raw, 1024) == {'schema': PROBE_REQUEST_SCHEMA, 'probe': 'time'}, fail)
        return found, None
    require(run.get('exit') == 0, fail)
    reply = decode_json(_stream(run, 'stdout', measured, found, fail), limits['output_bytes'])
    if real:
        descriptor = context['descriptor']
        request = _closed_request(request_raw, capability, descriptor, profile, context['input_value'])
        require(type(reply) is dict and reply.get('schema') == _pyc_check.RESPONSE_SCHEMA and
                reply.get('request_sha256') == sha256(request_raw) and
                reply.get('abi') == {'version': descriptor['python']['version'], 'magic': descriptor['python']['magic'],
                                     'machine': ARCH[descriptor['platform']][1]} and
                [v.get('cache') for v in reply.get('verdicts', [])] == [r['cache'] for r in request['rows']] and
                all(type(v.get('equal')) is bool for v in reply['verdicts']), fail)
        verdicts = [v['equal'] for v in reply['verdicts']]
        if kind == 'positive':
            require(all(verdicts), fail)
        else:
            positive = context['positive']
            changed = [i for i, (a, b) in enumerate(zip(positive['rows'], request['rows'])) if a != b]
            require(len(positive['rows']) == len(request['rows']) and len(changed) == 1 and
                    {k: v for k, v in request['rows'][changed[0]].items() if k != 'cache_b64'} ==
                    {k: v for k, v in positive['rows'][changed[0]].items() if k != 'cache_b64'} and
                    verdicts.count(False) == 1 and verdicts[changed[0]] is False, fail)
        return found, request
    require(decode_json(request_raw, 1024) == {'schema': PROBE_REQUEST_SCHEMA, 'probe': kind}, fail)
    require(type(reply) is dict and set(reply) == PROBE_OBSERVATION_KEYS[kind] and reply['probe'] == kind, fail)
    if kind == 'network':
        ok = reply['attempted'] == '1.1.1.1:443' and reply['interfaces'] == ['lo'] and reply['errno'] == 'ENETUNREACH'
    elif kind == 'rootfs_write':
        ok = reply['target'] == '/opt/cortex-inspector/canary' and reply['errno'] == 'EROFS' and reply['written'] is False
    elif kind == 'tmpfs':
        ok = (reply['target'] == '/tmp/cortex-probe' and reply['errno'] == 'EROFS' and reply['written'] is False and
              reply['tmpfs_on_tmp'] is False)
    elif kind == 'pids':
        # kai PID clause: the refused fork happened AT the admitted cap and the cgroup
        # pids controller attributed it (events max +1); an early EAGAIN refuses.
        ok = (reply['errno'] == 'EAGAIN' and reply['pids_max'] == limits['pids'] and
              reply['pids_current_at_denial'] == limits['pids'] and type(reply['pids_events_max_delta']) is int and
              reply['pids_events_max_delta'] == 1 and reply['reaped'] is True)  # exactly the one denied fork (kai)
    elif kind == 'memory':
        ok = (reply['memory_max'] == limits['memory_bytes'] and type(reply['attempted_bytes']) is int and
              reply['attempted_bytes'] > limits['memory_bytes'] and reply['child_signal'] == 9 and
              type(reply['oom_kill_delta']) is int and reply['oom_kill_delta'] >= 1)
    else:  # mounts_env: equality with the ACCEPTED allowlist, never "denied"
        allowlist = context['allowlist']
        ok = (type(allowlist) is dict and reply['mounts'] == sorted(allowlist.get('mounts', [None])) and
              reply['environment'] == sorted(allowlist.get('environment', [None])))
    require(ok, fail)
    return found, None


def _match_qualification(doc, pending, input_value, builds, measured, inventory, receipt_sha256, input_sha256):
    """Inside the native verifier only: re-derive every R6 row from its measured
    originals with qualification_row and bind it to the admitted closure, probe asset,
    engine, confs, host and accepted allowlist (Cox R4-R01-001, Vera R6-T1). Returns
    the context-bound match and the originals it consumed."""
    from pathlib import Path
    from .build import _owned_remove
    qualification_complete(doc)
    capability = pending.capability
    probe = pending.probe
    descriptor = thaw(capability.descriptor)['descriptor']
    profile = thaw(pending.profile)
    host = thaw(pending.host)
    fail = 'native_inspector_execution_unqualified'
    require(probe is not None and thaw(probe.descriptor).get('purpose') == 'probe' and
            doc['architecture'] == input_value['builder_requirements']['architecture'] and
            doc['platform'] == input_value['target']['oci_platform'] == descriptor['platform'] and
            doc['engine'] == input_value['builder_requirements']['engine'] and
            all(builds[slot]['observed_builder']['engine'] == doc['engine'] for slot in ('A', 'B')) and
            doc['engine_path'] == str(capability.engine) == str(probe.engine) and
            doc['engine']['sha256'] == capability.engine_sha256 == probe.engine_sha256 and
            doc['inspector']['closure_sha256'] == capability.closure_sha256 and
            doc['engine_config'] == {name: descriptor['engine_config'][name]['sha256'] for name in ('containers_conf', 'storage_conf')} and
            all(doc['host'][key] == host.get(key) for key in ('os', 'arch', 'uid', 'rootless')) and
            doc['probe_image'] == probe.image_digest != capability.image_digest and
            doc['cleanup_script']['sha256'] == sha256(Path(_owned_remove.__file__).read_bytes()), fail)
    rows = {row['path']: row for row in inventory if row['role'] == 'native.qualification.original'}
    found = {}

    def original(path):
        require(path in rows and path in measured, fail)
        found[path] = rows[path]
        return measured[path]

    context = {'capability': capability, 'probe': probe, 'descriptor': descriptor, 'profile': profile,
               'input_value': input_value, 'doc': doc, 'allowlist': thaw(pending.allowlist), 'positive': None}

    def row(kind, cited):
        children = decode_json(original(cited['children']), 8 * 1024 * 1024)
        streams, request = qualification_row(kind, original(cited['request']), children, measured, context)
        for path in streams:
            original(path)
        return request

    context['positive'] = row('positive', doc['inspector']['positive'])
    row('negative', doc['inspector']['negative'])
    for name in PROBES:
        row(name, doc['containment'][name])
    return MatchedQualification(pending, receipt_sha256, input_sha256, capability.closure_sha256, _SEAL), found


FULL_CHILD = {'argv', 'cwd', 'environment_names', 'exit', 'started_at_unix_ns', 'finished_at_unix_ns',
              'stdout', 'stderr', 'journal'}


def _native_children(build, measured, inventory, doc, input_value=None, pending=None):
    """Every phase's REQUIRED ordered children with full metadata and measured
    streams (Hale R4-MAP-001(c), Cox R4-R01-001): owned prefix, fixed engine
    environment, owned temporary path; the build child is prefix + admitted
    template in the bound clone cwd. An empty or reordered list refuses."""
    from .inspector import ENGINE_ENVIRONMENT
    rows = {row['path']: row for row in inventory}
    found = {}
    observed = build['observed_builder']
    store, runroot = observed['store_id'], observed['runroot_id']
    fail = 'native_child_evidence'
    require(store.endswith('-store') and runroot == store[:-len('-store')] + '-runroot', fail)
    stem = store[:-len('-store')]
    prefix = [doc['engine_path'], '--remote=false', '--root', store, '--runroot', runroot, '--tmpdir', stem + '-tmp']
    # The verifier always passes input_value: the inspect digest is the accepted base
    # config and the build suffix the admitted template. Without it (helper use) the
    # same sequence/metadata binding applies with the op's own recorded template.
    base = 'sha256:' + input_value['dependencies']['base_images'][0]['config_digest'] if input_value else None
    template = list(input_value['recipe']['argv_template']) if input_value else None
    expected = {
        'input': [['load', '--input', stem + '-tmp/base.tar'], ['image', 'inspect', '--format', 'json', base or '']],
        'parser': [['info', '--format', 'json']],
        'build': [template or []],
        'export': [['image', 'save', '--format', 'oci-dir', '--uncompressed', '--output', stem + '-layout/oci',
                    observed['container_id']]]}
    for op in build['operations']:
        path = op['evidence']['path']
        require(path.endswith('.json'), fail)
        path = path[:-5] + '.children.json'
        require(path in rows and path in measured, fail)
        found[path] = rows[path]
        children = decode_json(measured[path], 8 * 1024 * 1024)
        require(type(children) is list, fail)
        for child in children:
            require(type(child) is dict and FULL_CHILD <= set(child) and child['exit'] == 0 and
                    child['environment_names'] == sorted(ENGINE_ENVIRONMENT) and type(child['argv']) is list and
                    op['started_at_unix_ns'] <= child['started_at_unix_ns'] <= child['finished_at_unix_ns'] <=
                    op['finished_at_unix_ns'], fail)
            for key in ('stdout', 'stderr', 'journal', 'request'):
                if key == 'request' and key not in child:
                    continue
                stream = child[key]
                require(type(stream) is dict and stream.get('path') in rows and stream['path'] in measured and
                        stream.get('bytes') == len(measured[stream['path']]) and
                        stream.get('sha256') == sha256(measured[stream['path']]), fail)
                found[stream['path']] = rows[stream['path']]
        phase = op['phase']
        if phase in expected:
            wanted = [list(tail) for tail in expected[phase]]
            if phase == 'build' and template is None:
                wanted = [list(op['argv'])]
            if phase == 'input' and base is None and len(children) == 2:
                digest = children[1]['argv'][-1]
                require(digest.startswith('sha256:') and len(digest) == 71, fail)
                wanted[1][-1] = digest
            require([child['argv'] for child in children] == [prefix + tail for tail in wanted], fail)
            if phase == 'build':
                require(children[0]['cwd'] == observed['source_id'] and op['argv'] == wanted[0], fail)
        elif phase == 'cleanup':
            # complete discovery of the owned store, removal of exactly what it found,
            # positive absence, then the controlled deleter on the recorded roots
            listing = prefix + ['ps', '-a', '--external', '--format', 'json']
            require(len(children) in (3, 4) and children[0]['argv'] == listing and children[-2]['argv'] == listing, fail)
            found_ids = _container_ids(measured[children[0]['stdout']['path']])
            if len(children) == 4:
                require(bool(found_ids) and children[1]['argv'] == prefix + ['rm', '--force', '--time', '0', *found_ids], fail)
            else:
                require(found_ids == [], fail)
            require(_container_ids(measured[children[-2]['stdout']['path']]) == [], fail)
            _deleter(children[-1]['argv'], prefix, doc, [store, runroot])
        else:  # audit: [evidence call, its real nested R3 children]* (empty only without caches)
            require(not children or pending is not None, fail)
            index = 0
            while index < len(children):
                _audit_group(children[index:index + 7], measured, doc, pending, input_value)
                index += 7
    return found


def _container_ids(raw):
    rows = decode_json(raw or b'[]', 1024 * 1024, canonical=False)
    require(type(rows) is list and all(type(row) is dict and _hex(row.get('Id')) for row in rows), 'native_child_evidence')
    return sorted({row['Id'] for row in rows})


def _deleter(argv, prefix, doc, roots):
    """The one controlled namespace deleter: recorded interpreter, the repository's own
    _owned_remove.py (bound by sha in _match_qualification), exact owned roots + ids."""
    head = prefix + ['unshare', doc['cleanup_interpreter'], '-I', '-S', '-B', doc['cleanup_script']['path']]
    tail = argv[len(head):]
    require(argv[:len(head)] == head and len(tail) == 3 * len(roots) and
            [tail[3 * i] for i in range(len(roots))] == roots and
            all(tail[3 * i + j].isdigit() for i in range(len(roots)) for j in (1, 2)), 'native_child_evidence')


def _audit_group(group, measured, doc, pending, input_value):
    """One evidence call + its six real nested R3 children, bound to the admitted engine,
    inspector closure/plan, owned session/name, flags, limits, cwd and request/reply."""
    from .inspector import ARCH
    from . import _pyc_check
    fail = 'native_child_evidence'
    capability = pending.capability
    descriptor = thaw(capability.descriptor)['descriptor']
    profile = thaw(pending.profile)
    require(len(group) == 7 and 'request' in group[0], fail)
    call, nested = group[0], group[1:]
    session, _, run = _nested_group(nested, measured, doc, capability, profile['inspector']['limits'], fail)
    require(call['argv'] == run['argv'] and call['cwd'] == session, fail)
    try:
        request_raw = _stream(call, 'request', measured)
        request = _closed_request(request_raw, capability, descriptor, profile, input_value)
        reply = decode_json(_stream(call, 'stdout', measured), profile['inspector']['limits']['output_bytes'])
    except ValueError:
        raise ReceiptRefusal(fail) from None
    require(type(reply) is dict and reply.get('schema') == _pyc_check.RESPONSE_SCHEMA and
            reply.get('request_sha256') == sha256(request_raw) and
            reply.get('abi') == {'version': descriptor['python']['version'], 'magic': descriptor['python']['magic'],
                                 'machine': ARCH[descriptor['platform']][1]} and
            [v.get('cache') for v in reply.get('verdicts', [])] == [r['cache'] for r in request['rows']] and
            run['stdout']['sha256'] == call['stdout']['sha256'], fail)


def parse_build_receipt(raw_bytes):
    value = decode_json(raw_bytes, 8 * 1024 * 1024)
    schema_validate(value, 'build-receipt.schema.json')
    ordered_inventory(value['evidence']['inventory'])
    for build in value['builds'].values():
        ordered_inventory(build['outputs'])
    return ParsedBuildReceipt(raw_bytes, sha256(raw_bytes), freeze(value))


def _json_record(record, measured):
    require(record['media_type'] == 'application/json', 'evidence_media_type')
    require(record['path'] in measured, 'unindexed_evidence')
    return decode_json(measured[record['path']], 8 * 1024 * 1024)


def verify_build_receipt(receipt, verified_input, output_reader, evidence_reader, qualification_policy):
    require(isinstance(verified_input, VerifiedBuildInput) and verified_input._seal is _SEAL, 'unverified_input')
    require(isinstance(receipt, ParsedBuildReceipt), 'unparsed_receipt')
    policy = qualification_policy
    require(isinstance(policy, dict), 'invalid_policy_shape')
    scope = thaw(verified_input.document)['admission_policy']['qualification_scope']
    if scope == 'native':
        # Acceptance of exact original native bytes/policy precedes reparse,
        # readers, OCI/marshal inspection and any compiler authority.
        require(type(receipt.raw) is bytes and len(receipt.raw) <= 8*1024*1024 and
                policy.get('receipt_sha256') == sha256(receipt.raw), 'native_receipt_not_independently_accepted')
        require(policy.get('policy_sha256') == thaw(verified_input.document)['admission_policy']['policy_sha256'],
                'receipt_audit_policy')
        # Vera Q2 order, step 1: closure/ABI/host preflight only; no session yet.
        from .inspector import preflight
        pending = preflight(verified_input, policy)
    else:
        pending = None
    inspection = None
    parsed = parse_build_receipt(receipt.raw)
    require(parsed.sha256 == receipt.sha256 and parsed.document == receipt.document, 'changed_parsed_receipt')
    value, input_value = thaw(parsed.document), thaw(verified_input.document)
    policy = qualification_policy
    scope = input_value['admission_policy']['qualification_scope']
    require(value['input_sha256'] == verified_input.input_sha256 == policy.get('input_sha256') and
            value['target'] == thaw(verified_input.target), 'receipt_input_binding')
    require(value['qualification_scope'] == scope == policy.get('qualification_scope') and
            (scope != 'native' or verified_input.native_qualified), 'receipt_qualification_scope')
    require(value['result'] == 'PASS' and value['reason'] == 'complete', 'receipt_not_pass')
    audit = value['audit']
    require(audit['policy_sha256'] == policy.get('policy_sha256') == input_value['admission_policy']['policy_sha256'] and
            audit['policy_id'] == input_value['admission_policy']['policy_id'], 'receipt_audit_policy')
    require(audit['required_classes'] == list(CLASSES) == policy.get('required_classes') and not audit['findings'], 'audit_class_closure')
    limit = input_value['resources']['disk_bytes']
    inventory = value['evidence']['inventory']
    index_record = value['evidence']['index']
    require(index_record['role'] == 'evidence-index' and index_record['path'] not in [r['path'] for r in inventory], 'evidence_index_cycle')
    measured = {r['path']: measure(r, evidence_reader, min(limit, 8 * 1024 * 1024)) for r in inventory + [index_record]}
    require(_json_record(index_record, measured) == {'inventory': inventory}, 'evidence_index_closure')
    required_evidence, output_paths, builds = {}, set(), value['builds']
    if scope == 'native':
        # Steps 2-4: measured closed native roles, matched to the accepted context,
        # then the ONE session. Exact-set evidence closure stays below.
        rows = [row for row in inventory if row['role'] == 'native.qualification']
        require(len(rows) == 1, 'native_qualification_incomplete')
        record = rows[0]
        doc = decode_json(measured[record['path']], 1024 * 1024)
        matched, originals = _match_qualification(doc, pending, input_value, builds, measured, inventory,
                                                  parsed.sha256, verified_input.input_sha256)
        required_evidence.update(originals)
        for slot in ('A', 'B'):
            required_evidence.update(_native_children(builds[slot], measured, inventory, doc, input_value, pending))
        required_evidence[record['path']] = record
        from .inspector import admit_session
        inspection = admit_session(pending, matched)
    for slot in ('A', 'B'):
        observed = builds[slot]['observed_builder']
        require(observed['build_id'] == builds[slot]['build_id'], 'observed_build_binding')
        require(observed['qualification_scope'] == scope and observed['os'] == input_value['builder_requirements']['os'] and
                observed['architecture'] == input_value['builder_requirements']['architecture'] and
                observed['rootless'] and observed['uid'] >= input_value['builder_requirements']['uid_min'] and
                observed['toolchain'] == input_value['builder_requirements']['toolchain'] and
                observed['engine'] == input_value['builder_requirements']['engine'] and
                observed['fresh_store'] and not observed['mutable_shared'], 'observed_builder_requirements')
    require(all(builds['A']['observed_builder'][key] != builds['B']['observed_builder'][key] for key in IDENTITIES), 'independent_builds')
    archives, closures, semantic = [], [], []
    all_outputs = []
    for slot in ('A', 'B'):
        build = builds[slot]
        require(build['source_sha256'] == sha256(canonical_bytes(input_value['source'])) and
                build['recipe_sha256'] == sha256(canonical_bytes(input_value['recipe'])) and
                build['dependency_sha256'] == sha256(canonical_bytes(input_value['dependencies'])), 'build_input_components')
        start, finish = build['started_at_unix_ns'], build['finished_at_unix_ns']
        require(start <= finish and finish - start <= input_value['resources']['absolute_timeout_sec'] * 10**9, 'build_time_budget')
        operations = build['operations']
        require([op['phase'] for op in operations] == list(PHASES), 'executed_phase_closure')
        previous = start
        for op in operations:
            require(op['status'] == 'exited' and op['exit_code'] == 0 and
                    previous <= op['started_at_unix_ns'] <= op['finished_at_unix_ns'] <= finish and
                    op['finished_at_unix_ns'] - op['started_at_unix_ns'] <= input_value['resources']['phase_timeouts_sec'][op['phase']] * 10**9,
                    'operation_exit_or_budget')
            require(op['environment_names'] == input_value['determinism']['environment']['names'] and
                    op['environment_policy_sha256'] == audit['policy_sha256'], 'operation_environment_policy')
            if op['phase'] == 'build':
                require(op['argv'] == input_value['recipe']['argv_template'], 'operation_recipe_argv')
            record = op['evidence']
            expected = {k: v for k, v in op.items() if k != 'evidence'}
            expected.update(build_id=build['build_id'], qualification_scope=scope)
            require(_json_record(record, measured) == expected, 'operation_raw_evidence')
            required_evidence[record['path']] = record
            previous = op['finished_at_unix_ns']
        outputs = build['outputs']
        require(len({r['role'] for r in outputs}) == len(outputs) and
                not output_paths.intersection(r['path'] for r in outputs), 'output_role_or_path_alias')
        output_paths.update(r['path'] for r in outputs)
        actual = {r['path']: measure(r, output_reader, limit) for r in outputs}
        roles = {r['role']: r for r in outputs}
        require('archive' in roles and roles['archive']['media_type'] == 'application/x-tar', 'raw_archive_media')
        archive = actual[roles['archive']['path']]
        closure, layer_inventory = inspect_oci(archive, outputs, actual, build['oci'], input_value,
                                              module_policy=thaw(verified_input.metadata)['release_policy'],
                                              accepted_inputs=thaw(verified_input.metadata)['accepted_inputs'],
                                              inspection=inspection)
        require(build['provenance'] == roles['provenance'] and roles['provenance']['media_type'] == 'application/json', 'provenance_record_binding')
        provenance = decode_json(actual[roles['provenance']['path']], 8 * 1024 * 1024)
        expected = {key: build[key] for key in ('build_id','source_sha256','recipe_sha256','dependency_sha256','observed_builder')}
        expected.update(input_sha256=verified_input.input_sha256, output_sha256=sha256(archive),
                        policy_sha256=audit['policy_sha256'], qualification_scope=scope)
        require(provenance == expected, 'provenance_binding')
        require(roles['sbom']['media_type'] == 'application/json' and
                decode_json(actual[roles['sbom']['path']], 8 * 1024 * 1024) ==
                {'output_sha256': sha256(archive), 'inventory_sha256': sha256(canonical_bytes(layer_inventory))}, 'sbom_binding')
        require(set(audit['reports'][slot]) == set(CLASSES), 'semantic_report_closure')
        semantic_rows = []
        for category in CLASSES:
            record = audit['reports'][slot][category]
            report = _json_record(record, measured)
            require(report == {'build_id': build['build_id'], 'class': category, 'output_sha256': sha256(archive),
                               'inventory_sha256': sha256(canonical_bytes(layer_inventory)), 'findings': []}, 'semantic_report_binding')
            semantic_rows.append({k:v for k,v in report.items() if k != 'build_id'})
            required_evidence[record['path']] = record
        custody = build['custody']
        require(_json_record(custody, measured) == {'build_id': build['build_id'], 'outputs': outputs, 'complete': True}, 'build_custody')
        cleanup = build['cleanup']
        resources = [build['observed_builder'][key] for key in IDENTITIES if key != 'build_id']
        require(_json_record(cleanup, measured) == {'build_id': build['build_id'], 'removed_resources': resources,
                'remaining_owned_resources': [], 'complete': True}, 'build_cleanup')
        required_evidence[custody['path']] = custody
        required_evidence[cleanup['path']] = cleanup
        archives.append(archive)
        closures.append(closure)
        semantic.append(semantic_rows)
        all_outputs.extend(outputs)
    comparison = value['comparison']
    require(archives[0] == archives[1] and comparison == {'raw_archives_equal': True,
            'raw_archive_sha256': sha256(archives[0]), 'raw_archive_bytes': len(archives[0]),
            'oci_closure_equal': True, 'semantic_audit_equal': True} and closures[0] == closures[1] and
            semantic[0] == semantic[1], 'raw_equality_and_audit')
    custody, cleanup = value['custody'], value['cleanup']
    require(custody['complete'] and not custody['partial'] and not custody['missing_roles'] and
            _json_record(custody['receipt'], measured) == {'complete': True, 'partial': False, 'missing_roles': [],
            'outputs': sorted(all_outputs, key=lambda r:r['path'])}, 'complete_custody')
    require(cleanup['complete'] and not cleanup['remaining_owned_resources'] and
            _json_record(cleanup['receipt'], measured) == {'build_ids': [builds[s]['build_id'] for s in ('A','B')],
            'complete': True, 'remaining_owned_resources': []}, 'complete_cleanup')
    required_evidence[custody['receipt']['path']] = custody['receipt']
    required_evidence[cleanup['receipt']['path']] = cleanup['receipt']
    require(sorted(required_evidence.values(), key=lambda r:r['path']) == inventory, 'indexed_evidence_complete')
    if scope == 'native':
        require(policy.get('receipt_sha256') == parsed.sha256, 'native_receipt_not_independently_accepted')
    return VerifiedBuildReceipt(parsed.sha256, verified_input.input_sha256, freeze(value['target']),
                                sha256(archives[0]), freeze({p:sha256(b) for p,b in measured.items()}),
                                scope == 'native', freeze(value), _SEAL)


def verify_native_receipt_bytes(raw, verified_input, output_reader, evidence_reader, qualification_policy):
    """Bound exact original native receipt before even JSON/schema decoding."""
    require(isinstance(verified_input, VerifiedBuildInput) and verified_input._seal is _SEAL, 'unverified_input')
    require(isinstance(qualification_policy, dict) and type(raw) is bytes and len(raw)<=8*1024*1024 and
            qualification_policy.get('receipt_sha256')==sha256(raw), 'native_receipt_not_independently_accepted')
    require(qualification_policy.get('policy_sha256')==thaw(verified_input.document)['admission_policy']['policy_sha256'], 'receipt_audit_policy')
    # Vera Q2 step 1 before any decoding: closure/ABI/host eligibility only. No session
    # here; verify_build_receipt runs the one preflight/match/session order.
    from .inspector import preflight
    preflight(verified_input, qualification_policy)
    return verify_build_receipt(parse_build_receipt(raw), verified_input, output_reader, evidence_reader, qualification_policy)
