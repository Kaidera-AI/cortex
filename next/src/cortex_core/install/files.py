"""Private X11-b destination primitives; the installed X2/R01 supplier is UNBOUND.

The fixed supplier holds authenticated admitted inventory, the existing R01
RootReader, protected owner/exclusion and native ACL/xattr collectors through
publication. Its plan and observations are DATA, not grants. This module parses
no archive, accepts no caller-selected target/provider, publishes no P1 history,
and does not turn a synthetic supplier into native authorization. X11-d must join
this held measurement to X1's non-SQL persistence. Kernel IO that crosses the
original bound cannot receive a verified result or ACK.
"""
from contextlib import contextmanager
import hashlib
import os
import posixpath
import stat

from cortex_core.release.codec import canonical_bytes, relative_path
from cortex_core.release.contracts import freeze, thaw
from cortex_core.release.custody import RootReader
from . import owner
from .contracts import absolute_path

__all__ = []


def _need(value, reason='resource_changed'):
    owner._require(value, reason)


def _copy(value):
    raw = canonical_bytes(thaw(value))
    _need(len(raw) <= 8 * 1024**2, 'raw_limit')
    return freeze(thaw(value))


def _integer(value, minimum=0):
    return type(value) is int and minimum <= value < 2**63


def _path(value, *, basename=False):
    try:
        relative_path(value)
        _need(not basename or '/' not in value)
        return value
    except ValueError:
        raise owner.OwnerRefusal('resource_changed') from None


def _stable(info):
    return [info.st_dev, info.st_ino, info.st_uid, info.st_gid, info.st_mode]


def _full(info):
    return [*_stable(info), info.st_nlink, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def _binding(value):
    _need(type(value) is dict, 'required_binding_gap')
    absolute_path(value.get('root'))
    uid = value.get('owner_uid')
    _need(_integer(uid, 1) and _integer(value.get('owner_gid'))
          and _integer(value.get('peer_uid'), 1) and uid != value['peer_uid']
          and value.get('backend_uid') == uid, 'peer_denied')
    _need(type(value.get('root_identity')) is list and len(value['root_identity']) == 2
          and all(_integer(x, 1) for x in value['root_identity']), 'custody_changed')
    _need(type(value.get('clock_epoch')) is str and value['clock_epoch'], 'owner_unavailable')
    authority = value.get('authority')
    _need(type(authority) is list and len(authority) == 4
          and all(type(x) is str and x for x in authority[:3])
          and _integer(authority[3]), 'owner_unavailable')
    exclusion = value.get('exclusion')
    _need(type(exclusion) is dict and set(exclusion) == {'holder','generation','participants','bypass_uids'}
          and type(exclusion['holder']) is str and exclusion['holder']
          and _integer(exclusion['generation'], 1) and exclusion['participants'] == [uid]
          and exclusion['bypass_uids'] == [], 'atomicity_unavailable')
    _need(value.get('consumers') == [], 'resource_changed')


def _plan(p):
    _need(type(p) is dict and p.get('action') in ('create','materialize','replace','remove'))
    _path(p.get('target')); _path(p.get('stage'), basename=True)
    _need(p['stage'] != p['target'].split('/')[-1])
    _need(_integer(p.get('deadline_ns'), 1) and _integer(p.get('expanded_bytes'), 1)
          and _integer(p.get('member_count'), 1) and p['member_count'] <= 10000)
    _need(type(p.get('subject')) is dict and set(p['subject']) == {'identity','effect','request_ref'})
    _need(p.get('ownership') == 'created_by_this_transaction' and p.get('data_class') == 'ephemeral')
    if p['action'] in ('replace','remove'):
        _need(type(p.get('before')) is dict)
    else:
        _need(p.get('before') is None)
    if p['action'] == 'remove':
        _need(p.get('inverse') is True and p.get('after') is None)
        return
    _need(p.get('inverse') is False)
    flat = {}; total = 0
    def visit(node, path, depth):
        nonlocal total
        _need(type(node) is dict and depth <= 32)
        flat[path] = node
        _need(len(flat) <= p['member_count'], 'raw_limit')
        kind = node.get('kind')
        fields = {'file': {'kind','mode','bytes','sha256','source'},
                  'directory': {'kind','mode','children'}, 'link': {'kind','mode','target'}}
        _need(kind in fields and set(node) == fields[kind])
        _need(type(node['mode']) is int and node['mode'] in
              ({0o600,0o700} if kind == 'file' else {0o700} if kind == 'directory' else {0o777}))
        if kind == 'file':
            _path(node['source'])
            _need(_integer(node['bytes']) and type(node['sha256']) is str
                  and len(node['sha256']) == 64 and all(c in '0123456789abcdef' for c in node['sha256']))
            total += node['bytes']; _need(total <= p['expanded_bytes'], 'raw_limit')
        elif kind == 'directory':
            _need(type(node['children']) is dict)
            for name, child in sorted(node['children'].items()):
                _path(name, basename=True)
                visit(child, path+'/'+name if path else name, depth+1)
        else:
            target = node['target']
            _need(type(target) is str and 0 < len(target.encode()) <= 4096 and '\\' not in target
                  and '\x00' not in target and not target.startswith('/')
                  and posixpath.normpath(target) == target)
            resolved = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
            _need(resolved != '..' and not resolved.startswith('../'))
    visit(p.get('after'), '', 0)
    # R01 owns accepted link semantics. Defense in depth: an archive link may
    # name only an admitted non-link member, never a second traversed source.
    if p['after']['kind'] == 'directory':
        for path, node in flat.items():
            if node['kind'] == 'link':
                target = posixpath.normpath(posixpath.join(posixpath.dirname(path), node['target']))
                target = '' if target == '.' else target
                _need(target in flat and flat[target]['kind'] != 'link')
    if p['action'] == 'materialize':
        _need(p['after']['kind'] == 'directory')
    if p['action'] == 'replace' and p['before'].get('kind') == 'directory':
        # Portable rename can replace an empty directory. Nonempty directory
        # exchange needs a separately admitted native atomic-exchange binding.
        _need(p['before'].get('children') == {} and p['after']['kind'] == 'directory')


class _Primitive:
    def __init__(self, inputs, binding, plan, now):
        self.inputs = inputs; self.binding = _copy(binding); self.plan = _copy(plan)
        self.now = now; self.closed = False; self.fds = {}; self.links = []
        self.attempted = False; self.snapshotted = False; self.durable = False
        self.created = {}; self.deleted = False; self.postimage = None
        self.writers = set()
        self.reader = inputs.reader
        _need(type(self.reader) is RootReader and hasattr(self.reader, '_aggregate_files'), 'required_binding_gap')
        try:
            root = self._keep(os.open('/', os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW))
            for part in binding['root'].split('/')[1:]:
                root = self._directory(root, part, private=False)
            s = os.fstat(root)
            _need([s.st_dev,s.st_ino] == binding['root_identity'] and s.st_uid == binding['owner_uid']
                  and s.st_gid == binding['owner_gid'] and stat.S_IMODE(s.st_mode) == 0o700, 'custody_changed')
            self.device = s.st_dev; self.root = root
            parts = plan['target'].split('/'); parent = root
            for part in parts[:-1]:
                parent = self._directory(parent, part, private=True)
            self.parent = parent; self.name = parts[-1]
            self._current(); self._custody(); self._sources()
        except BaseException:
            self.close(); raise

    def close(self):
        self.closed = True
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()

    def _keep(self, fd):
        info = os.fstat(fd); key = (info.st_dev, info.st_ino, stat.S_IFMT(info.st_mode))
        if key in self.fds:
            os.close(fd); return self.fds[key]
        self.fds[key] = fd
        return fd

    def _current(self):
        _need(not self.closed, 'owner_unavailable')
        b, now = self.inputs.current(); _binding(b)
        _need(_copy(b) == self.binding and _copy(self.inputs.plan) == self.plan, 'custody_changed')
        _need(_integer(now) and self.now <= now < self.plan['deadline_ns'], 'deadline_exceeded')
        self.now = now

    def _meta(self, parent, name, fd):
        self._current()
        value = self.inputs.metadata(parent, name, fd)
        _need(type(value) is dict and set(value) == {'acl','xattrs'}
              and type(value['acl']) is list and type(value['xattrs']) is dict, 'custody_changed')
        result = thaw(_copy(value)); self._current()
        return result

    def _exact(self, parent, name):
        # Reject case/normalization aliases on filesystems which resolve them.
        with self._scan(parent) as entries:
            for entry in entries:
                self._current()
                if entry.name == name:
                    return
        raise owner.OwnerRefusal('custody_changed')

    @contextmanager
    def _scan(self, fd):
        # A dup shares the directory offset on macOS. A nested live-name check
        # must have its own open-file description, not disturb the outer walk.
        self._current()
        scan = os.open('.',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
        try:
            _need(_stable(os.fstat(scan)) == _stable(os.fstat(fd)), 'custody_changed')
            with os.scandir(scan) as entries:
                yield entries
        finally:
            os.close(scan)
        self._current()

    def _directory(self, parent, name, *, private):
        self._current(); self._exact(parent, name)
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
        fd = self._keep(os.open(name, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW, dir_fd=parent))
        held = os.fstat(fd)
        _need(_stable(before) == _stable(held) and stat.S_ISDIR(held.st_mode), 'custody_changed')
        _need(held.st_uid in (0,self.binding['owner_uid'])
              and (not held.st_mode & 0o022 or held.st_uid == 0 and held.st_mode & stat.S_ISVTX), 'custody_changed')
        if private:
            _need(held.st_dev == self.device and held.st_uid == self.binding['owner_uid']
                  and stat.S_IMODE(held.st_mode) == 0o700, 'custody_changed')
        meta = self._meta(parent,name,fd)
        self.links.append((parent,name,fd,_stable(held),meta))
        self._current()
        return fd

    def _custody(self):
        for parent,name,fd,identity,meta in self.links:
            self._current(); self._exact(parent,name)
            _need(_stable(os.stat(name,dir_fd=parent,follow_symlinks=False)) == identity
                  and _stable(os.fstat(fd)) == identity
                  and self._meta(parent,name,fd) == meta, 'custody_changed')
        self._current()

    def _sources(self):
        self._current()
        try:
            self.reader.aggregate_finish()
            def visit(node):
                if node is None: return
                if node['kind'] == 'file':
                    raw = self.reader.aggregate_bytes(node['source'])
                    _need(len(raw) == node['bytes'] and hashlib.sha256(raw).hexdigest() == node['sha256'], 'wrong_release')
                elif node['kind'] == 'directory':
                    for child in node['children'].values(): visit(child)
            visit(thaw(self.plan)['after'])
        except Exception:
            raise owner.OwnerRefusal('custody_changed') from None
        self._current()

    def _observe(self, parent, name):
        count = 0; total = 0
        def visit(parent, name, depth):
            nonlocal count,total
            self._current(); count += 1
            _need(count <= self.plan['member_count'] and depth <= 32, 'raw_limit')
            try:
                before = os.stat(name,dir_fd=parent,follow_symlinks=False)
            except FileNotFoundError:
                return None
            self._exact(parent,name)
            _need(before.st_dev == self.device and before.st_uid == self.binding['owner_uid']
                  and before.st_gid == self.binding['owner_gid'], 'custody_changed')
            kind = stat.S_IFMT(before.st_mode)
            if kind == stat.S_IFDIR:
                flags = os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
            elif kind == stat.S_IFREG:
                _need(before.st_nlink == 1, 'custody_changed')
                flags = os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK
            elif kind == stat.S_IFLNK:
                _need(before.st_nlink == 1, 'custody_changed')
                if hasattr(os,'O_SYMLINK'): flags = os.O_RDONLY|os.O_SYMLINK
                elif hasattr(os,'O_PATH'): flags = os.O_PATH|os.O_NOFOLLOW
                else: raise owner.OwnerRefusal('required_binding_gap')
            else:
                raise owner.OwnerRefusal('custody_changed')
            fd = self._keep(os.open(name,flags,dir_fd=parent))
            _need(_full(os.fstat(fd)) == _full(before), 'custody_changed')
            value = dict(dev=before.st_dev,ino=before.st_ino,uid=before.st_uid,gid=before.st_gid,
                mode=stat.S_IMODE(before.st_mode),nlink=before.st_nlink,
                mtime_ns=before.st_mtime_ns,ctime_ns=before.st_ctime_ns,**self._meta(parent,name,fd))
            if kind == stat.S_IFDIR:
                children = {}
                with self._scan(fd) as entries:
                    for entry in entries:
                        _path(entry.name,basename=True)
                        children[entry.name] = visit(fd,entry.name,depth+1)
                        _need(children[entry.name] is not None)
                value.update(kind='directory',children=dict(sorted(children.items())))
            elif kind == stat.S_IFLNK:
                value.update(kind='link',target=os.readlink(name,dir_fd=parent))
            else:
                total += before.st_size; _need(total <= self.plan['expanded_bytes'], 'raw_limit')
                digest = hashlib.sha256(); offset = 0
                while True:
                    self._current()
                    raw = os.pread(fd,min(65536,before.st_size+1-offset),offset)
                    if not raw: break
                    offset += len(raw); _need(offset <= before.st_size, 'custody_changed'); digest.update(raw)
                _need(offset == before.st_size, 'custody_changed')
                value.update(kind='file',bytes=offset,sha256=digest.hexdigest())
            self._exact(parent,name)
            _need(_full(os.stat(name,dir_fd=parent,follow_symlinks=False)) == _full(before)
                  and _full(os.fstat(fd)) == _full(before)
                  and self._meta(parent,name,fd) == {k:value[k] for k in ('acl','xattrs')}, 'custody_changed')
            self._current()
            return value
        answer = visit(parent,name,0); self._custody()
        return answer

    def _matches(self, actual, node):
        if node is None: return actual is None
        if actual is None or actual['kind'] != node['kind'] or actual['mode'] != node['mode']:
            return False
        if actual['acl'] != [] or actual['xattrs'] != {}:
            return False
        if node['kind'] == 'file':
            return actual['bytes'] == node['bytes'] and actual['sha256'] == node['sha256']
        if node['kind'] == 'link': return actual['target'] == node['target']
        return set(actual['children']) == set(node['children']) and all(
            self._matches(actual['children'][name],child) for name,child in node['children'].items())

    @staticmethod
    def _identities(actual, path=''):
        if actual is None: return {}
        result = {path: [actual['dev'],actual['ino']]}
        for name,child in actual.get('children',{}).items():
            result.update(_Primitive._identities(child,path+'/'+name if path else name))
        return result

    def snapshot(self):
        _need(not self.snapshotted and not self.attempted, 'attempt_ambiguous')
        self._current(); self._sources()
        actual = self._observe(self.parent,self.name)
        _need(actual == thaw(self.plan)['before'])
        _need(self._observe(self.parent,self.plan['stage']) is None)
        self.preimage = _copy(actual); self.snapshotted = True
        return _copy(actual)

    def _build(self, parent, name, node, path=''):
        self._current(); kind = node['kind']; fd = None
        if kind == 'directory':
            os.mkdir(name,0o700,dir_fd=parent)
            fd = self._keep(os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent))
        elif kind == 'link':
            os.symlink(node['target'],name,dir_fd=parent)
            # Hold the generated link itself; never traverse its target.
            flags = (os.O_RDONLY|os.O_SYMLINK) if hasattr(os,'O_SYMLINK') else (getattr(os,'O_PATH',0)|os.O_NOFOLLOW)
            link = self._keep(os.open(name,flags,dir_fd=parent))
            s = os.fstat(link); self.created[path] = [s.st_dev,s.st_ino]
            if stat.S_IMODE(s.st_mode) != node['mode']:
                _need(hasattr(os,'O_SYMLINK'), 'required_binding_gap')
                self._current(); os.fchmod(link,node['mode']); self._current()
            observed = self._observe(parent,name)
            _need([observed['dev'],observed['ino']] == self.created[path])
            self._current(); return
        else:
            fd = os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,node['mode'],dir_fd=parent)
        try:
            s = os.fstat(fd); self.created[path] = [s.st_dev,s.st_ino]
            self._current()
            if kind == 'file':
                self.writers.add(fd)
                held = self._keep(os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent))
                _need(_full(os.fstat(held)) == _full(s), 'custody_changed')
                raw = self.reader.aggregate_bytes(node['source']); self._current()
                _need(len(raw) == node['bytes'] and hashlib.sha256(raw).hexdigest() == node['sha256'], 'wrong_release')
                for start in range(0,len(raw),65536):
                    view = memoryview(raw)[start:start+65536]
                    while view:
                        self._current(); n = os.write(fd,view); self._current()
                        _need(0 < n <= len(view)); view = view[n:]
                os.fchmod(fd,node['mode'])
            else:
                os.fchmod(fd,node['mode']); self._current()
                for child, spec in sorted(node['children'].items()):
                    self._build(fd,child,spec,path+'/'+child if path else child)
            self._current(); os.fsync(fd); self._current()
        finally:
            if kind == 'file':
                os.close(fd); self.writers.discard(fd)

    def _remove(self, parent, name, expected):
        self._current(); _need(self._observe(parent,name) == expected)
        if expected['kind'] == 'directory':
            fd = self._keep(os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent))
            for child, value in expected['children'].items():
                self._remove(fd,child,value)
            self._current(); _need(not os.listdir(fd))
            os.fsync(fd); self._current()
            _need([os.stat(name,dir_fd=parent,follow_symlinks=False).st_dev,
                   os.stat(name,dir_fd=parent,follow_symlinks=False).st_ino] == [expected['dev'],expected['ino']])
            os.rmdir(name,dir_fd=parent)
        else:
            os.unlink(name,dir_fd=parent)
        self._current(); os.fsync(parent); self._current()

    def _finish(self):
        self._current(); self._sources()
        actual = self._observe(self.parent,self.name)
        _need(not self.writers and self._observe(self.parent,self.plan['stage']) is None)
        _need(self._matches(actual,thaw(self.plan)['after']))
        _need(self.deleted if self.plan['action'] == 'remove' else
              self._identities(actual) == self.created, 'attempt_ambiguous')
        self._current(); os.fsync(self.parent); self._current()
        final = self._observe(self.parent,self.name)
        _need(final == actual)
        self.postimage = _copy(final)
        self._current(); self.durable = True

    def dispatch(self, context):
        _need(context == thaw(self.plan)['subject'] and self.snapshotted and not self.attempted, 'attempt_ambiguous')
        self._current(); self._sources(); self._custody()
        _need(_copy(self._observe(self.parent,self.name)) == self.preimage)
        _need(self._observe(self.parent,self.plan['stage']) is None)
        self._current(); self.attempted = True
        try:
            if self.plan['action'] == 'remove':
                self._remove(self.parent,self.name,thaw(self.preimage)); self.deleted = True
            else:
                self._build(self.parent,self.plan['stage'],thaw(self.plan)['after'])
                stage = self._observe(self.parent,self.plan['stage'])
                _need(self._matches(stage,thaw(self.plan)['after']) and self._identities(stage) == self.created)
                self._sources(); self._custody()
                _need(_copy(self._observe(self.parent,self.name)) == self.preimage)
                self._current()
                os.rename(self.plan['stage'],self.name,src_dir_fd=self.parent,dst_dir_fd=self.parent)
        except Exception:
            # Replies/exceptions never establish an outcome. Partial stages stay
            # retained; the independent held observation below decides.
            pass
        try:
            self._finish()
        except Exception:
            self.durable = False

    @contextmanager
    def measure(self, context):
        answer = ('UNKNOWN',None)
        actual = None
        try:
            _need(context == thaw(self.plan)['subject'] and self.attempted, 'attempt_ambiguous')
            self._current(); self._sources()
            actual = _copy(self._observe(self.parent,self.name))
            _need(not self.writers and self._observe(self.parent,self.plan['stage']) is None)
            state = None
            if self.durable and actual == self.postimage:
                state = 'APPLIED'
            elif not self.created and not self.deleted and actual == self.preimage:
                state = 'NOT_APPLIED'
            if state is not None:
                p = thaw(self.plan)
                result = {**p['subject'], 'state': {'APPLIED':'applied_verified',
                    'NOT_APPLIED':'not_applied_verified'}[state], 'observed': {
                    'resource': thaw(actual), 'backend': thaw(self.binding),
                    'cleanup': {'stage_absent':True, 'writers_closed':True}}}
                self._current(); answer = (state,canonical_bytes(result))
        except Exception:
            answer = ('UNKNOWN',None)
        # Publisher exceptions propagate. Exclusion/FDs survive the caller's
        # durable first-result publication and ACK; final IO is followed by E.
        yield answer
        if answer[0] != 'UNKNOWN':
            self._current(); self._sources()
            _need(_copy(self._observe(self.parent,self.name)) == actual)
            _need(self._observe(self.parent,self.plan['stage']) is None)
            self._custody(); self._current()


def _installed_files():
    raise owner.OwnerRefusal('required_binding_gap')


@contextmanager
def _open():
    inputs = _installed_files()
    with inputs.hold():
        binding, now = inputs.current(); _binding(binding)
        plan = thaw(_copy(inputs.plan)); _plan(plan)
        _need(_integer(now) and now < plan['deadline_ns'], 'deadline_exceeded')
        primitive = _Primitive(inputs,binding,plan,now)
        try:
            yield primitive
        finally:
            primitive.close()
