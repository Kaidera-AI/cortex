"""Linux source-contract delta for the ONE private B1 owner observer.

No kernel collector is installed here. The fixed supplier remains UNBOUND before
candidate access; these predicates cannot turn caller data into native evidence.
A future protected collector must bind the complete original graph, SO_PEERCRED
connection-time credentials AND current process lifetime/privileges, namespace
identities/maps, held objects and the whole executed closure independently of C.

The conservative profile uses pathname Unix stream sockets, ID-preserving maps
in V's admitted namespace, and no default ACLs. Source positives qualify DATA
only. Actual roots, accounts, namespaces, capabilities, OS and image/R01 proofs
still need separate native qualification. No syscall, repair, launch or effect.
"""
from pathlib import PurePosixPath
import re

from .. import owner

__all__ = []
_MAX = 2**63 - 1
_UID_MAX = 2**32 - 2
_NS = frozenset(('user', 'pid', 'mnt', 'net', 'ipc', 'uts', 'cgroup', 'time'))
_CAPS = frozenset(('inheritable', 'permitted', 'effective', 'ambient', 'bounding'))
_PROBES = frozenset(('ptrace', 'proc_mem', 'process_vm_writev', 'setns', 'setuid',
                     'file_capability', 'launch_write', 'backend_bypass'))
_LIFE = frozenset(('pid', 'boot_id', 'start_ticks', 'pidfd_id'))
_PROCESS = _LIFE | {'uids', 'gids', 'groups', 'code_sha256'}
_POLICY = frozenset(('version', 'peer_uid', 'peer_gid', 'peer_groups', 'owner_uid', 'owner_gid',
    'owner_groups', 'peer_process', 'owner_process', 'namespaces', 'maps', 'capabilities',
    'endpoint', 'objects', 'peer_code_sha256'))
_RECEIPT = frozenset(('version', 'host_os', 'owner_epoch', 'session_id', 'peer_lifetime',
    'host_identity_ref', 'transport', 'peer_process', 'owner_process', 'namespaces', 'maps',
    'privileges', 'startup', 'objects'))
_ROLES = frozenset(('endpoint', 'interpreter', 'source', 'import', 'native', 'schema',
                    'policy', 'state', 'journal', 'lock', 'originals', 'launch'))
_CODE = frozenset(('interpreter', 'source', 'import', 'native'))


def _installed_linux_inputs():
    # ONE fixed private supplier. No caller binder, path, FD, clock, policy,
    # environment switch, trusted flag or production test mode.
    raise owner.OwnerRefusal('required_binding_gap')  # BL-G00


def _closed(value, fields):
    return type(value) is dict and set(value) == fields


def _number(value, maximum=_MAX, minimum=0):
    return type(value) is int and minimum <= value <= maximum


def _text(value, limit=128):
    return type(value) is str and 0 < len(value.encode('utf-8')) <= limit


def _digest(value):
    return type(value) is str and re.fullmatch('[0-9a-f]{64}', value) is not None


def _ids(value, size=None):
    return (type(value) is list and 0 < len(value) <= 256
            and (size is None or len(value) == size)
            and all(_number(x, _UID_MAX) for x in value))


def _lifetime(value):
    return (_closed(value, _LIFE) and _number(value['pid'], 2**31-1, 1)
            and _number(value['start_ticks'], minimum=1)
            and _text(value['boot_id']) and _text(value['pidfd_id']))


def _process(value):
    return (_closed(value, _PROCESS) and _lifetime({k:value[k] for k in _LIFE})
            and _ids(value['uids'], 4) and _ids(value['gids'], 4)
            and _ids(value['groups']) and len(set(value['groups'])) == len(value['groups'])
            and _digest(value['code_sha256']))


def _namespaces(value):
    return (_closed(value, {'peer', 'owner'}) and all(_closed(v, _NS)
            and all(type(identity) is list and len(identity) == 2
                    and all(_number(n, minimum=1) for n in identity) for identity in v.values())
            for v in value.values()))


def _map_shape(value):
    return (_closed(value, {'peer', 'owner'}) and all(_closed(v, {'uid', 'gid'})
            and all(type(rows) is list and 0 < len(rows) <= 340
                    and all(type(row) is list and len(row) == 3
                            and all(_number(n, 2**32-1) for n in row) and row[2] > 0
                            for row in rows) for rows in v.values()) for v in value.values()))


def _cap_shape(value):
    return _closed(value, _CAPS) and all(_number(x) for x in value.values())


def _object_shape(value, *, observed):
    fields = {'path', 'kind', 'identity', 'held_identity', 'sha256', 'uid', 'gid', 'mode',
              'acl', 'default_acl', 'nlink'} if observed else {'path', 'kind', 'role', 'identity', 'sha256'}
    if not _closed(value, fields):
        return False
    if not (_text(value['path'], 4096) and _text(value['kind'])
            and type(value['identity']) is list and len(value['identity']) == 3
            and all(_number(n, minimum=1) for n in value['identity'])
            and (value['sha256'] is None or _digest(value['sha256']))):
        return False
    if not observed:
        return _text(value['role'])
    return (type(value['held_identity']) is list and len(value['held_identity']) == 3
            and all(_number(n, minimum=1) for n in value['held_identity'])
            and _number(value['uid'], _UID_MAX) and _number(value['gid'], _UID_MAX)
            and _number(value['mode'], 0o7777) and _number(value['nlink'], minimum=1)
            and type(value['acl']) is list and len(value['acl']) <= 259
            and type(value['default_acl']) is list and len(value['default_acl']) <= 259)


def _shape(policy, receipt):
    """Closed bounded source records; all failures use the existing C0 detail."""
    try:
        if not (_closed(policy, _POLICY) and _closed(receipt, _RECEIPT)):
            return False
        if not (_number(policy['version'], 1, 1) and _number(receipt['version'], 1, 1)
                and all(_number(policy[k], _UID_MAX) for k in ('peer_uid','peer_gid','owner_uid','owner_gid'))
                and all(_ids(policy[k]) and len(set(policy[k])) == len(policy[k])
                        for k in ('peer_groups','owner_groups'))
                and _lifetime(policy['peer_process']) and _lifetime(policy['owner_process'])
                and _process(receipt['peer_process']) and _process(receipt['owner_process'])
                and all(_namespaces(v['namespaces']) and _map_shape(v['maps']) for v in (policy,receipt))
                and _cap_shape(policy['capabilities']) and _digest(policy['peer_code_sha256'])
                and _text(policy['endpoint'], 107)
                and all(_text(receipt[k]) for k in ('host_os','owner_epoch','session_id','peer_lifetime'))
                and type(receipt['host_identity_ref']) is dict):
            return False
        transport = receipt['transport']; privileges = receipt['privileges']; startup = receipt['startup']
        return (_closed(transport, {'family','type','kind','source','endpoint','credentials'})
                and all(_text(transport[k]) for k in ('family','type','kind','source'))
                and _text(transport['endpoint'], 107)
                and _closed(transport['credentials'], {'pid','uid','gid'})
                and _number(transport['credentials']['pid'], 2**31-1, 1)
                and all(_number(transport['credentials'][k], _UID_MAX) for k in ('uid','gid'))
                and _closed(privileges, {'capabilities','no_new_privs','peer_lifetime','owner_lifetime','paths'})
                and _cap_shape(privileges['capabilities']) and type(privileges['no_new_privs']) is bool
                and all(_lifetime(privileges[k]) for k in ('peer_lifetime','owner_lifetime'))
                and _closed(privileges['paths'], _PROBES) and all(_text(v) for v in privileges['paths'].values())
                and _closed(startup, {'executable_path','python','crypto','dependencies','isolated','no_site','environment','pth',
                                     'candidate_imports','loaded_paths','native_paths'})
                and _text(startup['executable_path'],4096)
                and all(_text(startup[k]) for k in ('python','crypto'))
                and all(type(startup[k]) is bool for k in ('isolated','no_site'))
                and all(type(startup[k]) is list and len(startup[k]) <= 256
                        and all(_text(v,4096) for v in startup[k]) for k in
                        ('dependencies','environment','pth','candidate_imports','loaded_paths','native_paths'))
                and all(type(v['objects']) is list and 0 < len(v['objects']) <= 64 for v in (policy,receipt))
                and all(_object_shape(v,observed=False) for v in policy['objects'])
                and all(_object_shape(v,observed=True) for v in receipt['objects']))
    except (KeyError, TypeError, ValueError, UnicodeError):
        return False


def _mapped(rows, inside):
    # Both sides must be unambiguous; overlapping or overflowing ranges refuse.
    for i, (start, outside, size) in enumerate(rows):
        if start + size > 2**32 or outside + size > 2**32:
            return None
        for other, outer, length in rows[:i]:
            if max(start,other) < min(start+size,other+length) or max(outside,outer) < min(outside+size,outer+length):
                return None
    found = [outside + inside - start for start,outside,size in rows if start <= inside < start+size]
    return found[0] if len(found) == 1 else None


def _acl_valid(obj):
    entries = obj['acl']
    if not entries:
        return True  # Collector normalizes an absent extended ACL to [].
    seen = set(); base = {}; named = False
    for entry in entries:
        if not _closed(entry, {'tag','id','perm'}) or not _number(entry['perm'],7):
            return False
        tag = entry['tag']; ident = entry['id']
        if type(tag) is not str or tag not in ('owner','group','other','mask','user','named_group'):
            return False
        if tag in ('user','named_group'):
            if not _number(ident,_UID_MAX):
                return False
            named = True
        elif ident is not None:
            return False
        key = (tag,ident)
        if key in seen:
            return False
        seen.add(key)
        if tag not in ('user','named_group'):
            base[tag] = entry['perm']
    return ({'owner','group','other'} <= set(base) and (not named or 'mask' in base)
            and base['owner'] == (obj['mode'] >> 6) & 7
            and base.get('mask',base['group']) == (obj['mode'] >> 3) & 7
            and base['other'] == obj['mode'] & 7)


def _access(obj, uid, groups):
    """POSIX access ACL single-bit write/search queries; matched entries do not fall through."""
    if not obj['acl']:
        shift = 6 if obj['uid'] == uid else 3 if obj['gid'] in groups else 0
        return (obj['mode'] >> shift) & 7
    base = {e['tag']:e['perm'] for e in obj['acl'] if e['tag'] in ('owner','group','other','mask')}
    if obj['uid'] == uid:
        return base['owner']
    mask = base.get('mask',7)
    for entry in obj['acl']:
        if entry['tag'] == 'user' and entry['id'] == uid:
            return entry['perm'] & mask
    matched = obj['gid'] in groups
    perms = base['group'] if matched else 0
    for entry in obj['acl']:
        if entry['tag'] == 'named_group' and entry['id'] in groups:
            matched = True
            perms |= entry['perm']
    return perms & mask if matched else base['other']


def _installed_owner_inputs():
    """The common B1 observer shape: zero arguments -> exact existing _Inputs DATA."""
    inputs = _installed_linux_inputs()
    owner._require(type(inputs) is owner._Inputs and type(inputs.originals) is dict
        and len(inputs.originals) <= 256 and all(type(v) is bytes for v in inputs.originals.values())
        and sum(len(v) for v in inputs.originals.values()) <= 8*1024*1024, 'owner_unavailable')  # BL-G01
    context = owner._decode(inputs.context_raw, 'owner_unavailable')
    owner._require(set(context) == owner._CONTEXT, 'owner_unavailable')  # BL-G02
    peer = owner._original(inputs, context['peer_ref'])
    policy_original = owner._original(inputs, context['policy_ref'])
    owner._original(inputs, context['current_state_ref'])  # bind the observed state file to B1's original
    receipt = owner._original(inputs, peer.get('linux_ref'))
    policy = policy_original.get('linux')
    owner._require(_shape(policy, receipt), 'owner_unavailable')  # BL-G03
    c = receipt['peer_process']; v = receipt['owner_process']; t = receipt['transport']
    owner._require(receipt['host_os'] == 'linux' and type(context['target']) is dict
        and context['target'].get('host_os') == 'linux' and receipt['owner_epoch'] == context['owner_epoch'] == peer.get('owner_epoch')
        and receipt['session_id'] == context['session_id'] == peer.get('session_id')
        and receipt['peer_lifetime'] == peer.get('lifetime')
        and receipt['host_identity_ref'] == peer.get('host_identity_ref'), 'peer_denied')  # BL-G04
    owner._require(t['family'] == 'AF_UNIX' and t['type'] == 'SOCK_STREAM' and t['kind'] == 'pathname'
        and t['source'] == 'SO_PEERCRED' and t['endpoint'] == policy['endpoint'], 'peer_denied')  # BL-G05
    owner._require(t['credentials'] == {'pid':c['pid'],'uid':c['uids'][0],'gid':c['gids'][0]}, 'peer_denied')  # BL-G06
    owner._require({k:c[k] for k in _LIFE} == policy['peer_process'], 'peer_denied')  # BL-G07
    owner._require(c['uids'] == [policy['peer_uid']]*4 and c['gids'] == [policy['peer_gid']]*4
        and sorted(c['groups']) == sorted(policy['peer_groups']) and policy['peer_gid'] in c['groups']
        and type(peer.get('owner_uid')) is int and peer['owner_uid'] == policy['peer_uid'], 'peer_denied')  # BL-G08
    owner._require(policy['owner_uid'] != 0 and policy['peer_uid'] != 0
        and policy['owner_uid'] != policy['peer_uid'] and policy['owner_gid'] not in c['groups']
        and v['uids'] == [policy['owner_uid']]*4 and v['gids'] == [policy['owner_gid']]*4
        and sorted(v['groups']) == sorted(policy['owner_groups']) and policy['owner_gid'] in v['groups'], 'peer_denied')  # BL-G09
    owner._require({k:v[k] for k in _LIFE} == policy['owner_process']
        and policy['owner_process'] != policy['peer_process'] and v['boot_id'] == c['boot_id'], 'peer_denied')  # BL-G10
    owner._require(receipt['namespaces'] == policy['namespaces'], 'peer_denied')  # BL-G11
    owner._require(receipt['maps'] == policy['maps'], 'peer_denied')  # BL-G12
    owner._require(all(_mapped(receipt['maps'][who]['uid'], policy[who+'_uid']) == policy[who+'_uid']
        and all(_mapped(receipt['maps'][who]['gid'], gid) == gid for gid in policy[who+'_groups'])
        for who in ('peer','owner')), 'peer_denied')  # BL-G13
    privileges = receipt['privileges']
    owner._require(privileges['no_new_privs'] and privileges['capabilities'] == policy['capabilities']
        and all(privileges['capabilities'][k] == 0 for k in ('inheritable','permitted','effective','ambient')), 'peer_denied')  # BL-G14
    owner._require(privileges['peer_lifetime'] == policy['peer_process']
        and privileges['owner_lifetime'] == policy['owner_process']
        and all(v == 'denied' for v in privileges['paths'].values()), 'peer_denied')  # BL-G15
    owner._require(c['code_sha256'] == policy['peer_code_sha256'], 'peer_denied')  # BL-G29
    expected = policy['objects']; observed = receipt['objects']; startup = receipt['startup']
    owner._require(startup['executable_path'] in [x['path'] for x in expected if x['role'] == 'interpreter']
        and [x['sha256'] for x in expected if x['role'] == 'interpreter'] == [v['code_sha256']]
        and startup == dict(executable_path=startup['executable_path'], python='3.12.15', crypto='cryptography-50.0.0',
        dependencies=['cffi-2.1.1','pycparser-3.0'], isolated=True, no_site=True,
        environment=[], pth=[], candidate_imports=[],
        loaded_paths=[x['path'] for x in expected if x['role'] in _CODE],
        native_paths=[x['path'] for x in expected if x['role'] == 'native']), 'custody_changed')  # BL-G16
    owner._require(_ROLES <= {x['role'] for x in expected} <= _ROLES | {'root','ancestor'}
        and len({x['path'] for x in expected}) == len(expected)
        and len({tuple(x['identity']) for x in expected}) == len(expected)
        and [x['path'] for x in observed] == [x['path'] for x in expected]
        and [x['path'] for x in expected if x['role'] == 'endpoint'] == [policy['endpoint']], 'custody_changed')  # BL-G17
    owner._require(all(x['path'].startswith('/') and '\\' not in x['path'] and '\x00' not in x['path']
        and str(PurePosixPath(x['path'])) == x['path'] and '//' not in x['path']
        and '..' not in PurePosixPath(x['path']).parts for x in expected), 'custody_changed')  # BL-G18
    by_path = {x['path']:x for x in observed}
    owner._require(all(all(str(parent) in by_path and by_path[str(parent)]['kind'] == 'directory'
        for parent in PurePosixPath(x['path']).parents) for x in expected), 'custody_changed')  # BL-G19
    owner._require(all(a['kind'] == b['kind'] == ('socket' if a['role'] == 'endpoint'
        else 'directory' if a['role'] in ('root','ancestor','originals') else 'file')
        and (b['nlink'] == 1 or a['kind'] == 'directory') for a,b in zip(expected,observed)), 'custody_changed')  # BL-G20
    owner._require(all(a['identity'] == b['identity'] == b['held_identity'] for a,b in zip(expected,observed)), 'custody_changed')  # BL-G21
    owner._require(all((a['sha256'] is None and b['sha256'] == context[
        'policy_ref' if a['role'] == 'policy' else 'current_state_ref']['sha256'])
        if a['role'] in ('policy','state') else
        (a['sha256'] == b['sha256'] and (a['sha256'] is not None if a['kind'] == 'file'
        else a['sha256'] is None)) for a,b in zip(expected,observed)), 'custody_changed')  # BL-G22
    owner._require(all(x['uid'] in (0,policy['owner_uid']) for x in observed), 'custody_changed')  # BL-G23
    owner._require(all(x['mode'] & 0o7000 == 0 for x in observed), 'custody_changed')  # BL-G24
    owner._require(all(_acl_valid(x) for x in observed), 'custody_changed')  # BL-G25
    owner._require(all(not (_access(x,policy['peer_uid'],c['groups']) & 2)
        for x in observed if x['path'] != policy['endpoint']), 'custody_changed')  # BL-G26
    owner._require(all(not x['default_acl'] for x in observed), 'custody_changed')  # BL-G27
    endpoint = by_path[policy['endpoint']]
    owner._require(endpoint['kind'] == 'socket' and _access(endpoint,policy['peer_uid'],c['groups']) & 2
        and all(_access(by_path[str(parent)],policy['peer_uid'],c['groups']) & 1
                for parent in PurePosixPath(policy['endpoint']).parents), 'custody_changed')  # BL-G28
    return inputs
