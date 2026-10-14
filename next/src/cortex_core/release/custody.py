"""Physical read roots stay outside the key. No extraction or link traversal."""
import os
from pathlib import Path
import stat
import time
import hashlib
import re
from cortex_core.runtime.signed_member import Refusal
from .codec import relative_path
from .inputs import FileObservation


class RootReader:
    def __init__(self, root, limit):
        self.root = Path(root).resolve(strict=True)
        self.limit = limit
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        self.aggregate_release()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def _parent(self, path):
        parts = relative_path(path).split('/')
        fd = os.dup(self.fd)
        try:
            for part in parts[:-1]:
                next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = next_fd
            return fd, parts[-1]
        except BaseException:
            os.close(fd)
            raise

    @classmethod
    def for_aggregate(cls, root, limit):
        """Owner-internal root, kept lexical until the aggregate transaction.

        Does not resolve/open a candidate or change the legacy reader semantics.
        The production assembler is unbound before this reader is constructed.
        """
        reader=cls.__new__(cls)
        reader.root=Path(root);reader._aggregate_root_spec=os.fspath(root)
        reader.limit=limit;reader.fd=None;reader._aggregate_files={}
        reader._aggregate_dirs={};reader._aggregate_links=[]
        return reader

    def aggregate_release(self):
        for item in getattr(self,'_aggregate_files',{}).values():os.close(item[0])
        for fd in getattr(self,'_aggregate_dirs',{}).values():os.close(fd)
        self._aggregate_files={};self._aggregate_dirs={};self._aggregate_links=[]

    @staticmethod
    def _aggregate_path(path):
        if (type(path) is not str or len(path.encode('utf-8'))>4096
                or re.fullmatch(r'(?:members/[a-z0-9][a-z0-9._-]*(?:/[a-z0-9][a-z0-9._-]*)*|runtime\.lock\.json)',path) is None
                or any(p in ('.','..') for p in path.split('/'))):
            raise Refusal('member_path_alias')
        return path

    @staticmethod
    def _aggregate_identity(info):
        return (info.st_dev,info.st_ino,info.st_mode,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)

    @staticmethod
    def _aggregate_regular(info):
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:raise Refusal('member_path_alias')

    def _aggregate_device(self,info):
        if info.st_dev!=self._aggregate_dev:raise Refusal('member_path_alias')

    @staticmethod
    def _aggregate_exact_name(fd,name):
        if name not in {entry.name for entry in os.scandir(fd)}:
            try:os.stat(name,dir_fd=fd,follow_symlinks=False)
            except FileNotFoundError:
                raise Refusal('required_binding_gap') from None
            raise Refusal('member_path_alias')

    def _aggregate_directory(self,parent,name,key):
        self._aggregate_exact_name(parent,name)
        before=os.stat(name,dir_fd=parent,follow_symlinks=False)
        if key.startswith('relative:'):self._aggregate_device(before)
        fd=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
        self._aggregate_dirs[key]=fd
        self._aggregate_links.append((parent,name,fd,before.st_dev,before.st_ino))
        return fd

    def _aggregate_time(self):
        if time.monotonic()>self._aggregate_deadline:raise Refusal('raw_limit')

    def aggregate_begin(self, rows, limits, *, overhead_bytes=0):
        """Hold every named original and ancestor; no body is admitted here."""
        self.close()
        try:
            spec=getattr(self,'_aggregate_root_spec',None)
            if (type(spec) is not str or not spec.startswith('/') or '\\' in spec
                    or any(p in ('','.','..') for p in spec.split('/')[1:])):
                raise Refusal('member_path_alias')
            paths=[self._aggregate_path(row['path']) for row in rows]
            if len(paths)>limits['member_count']:raise Refusal('raw_limit')
            if len(set(paths))!=len(paths):raise Refusal('aggregate_shape')
            self._aggregate_limits=limits;self._aggregate_deadline=time.monotonic()+limits['read_seconds']
            self._aggregate_expected=set(paths);self._aggregate_total=overhead_bytes
            parent=os.open('/',os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
            self._aggregate_dirs['/']=parent
            key=''
            for part in spec.split('/')[1:]:
                self._aggregate_time();key+='/'+part;parent=self._aggregate_directory(parent,part,key)
            self.fd=os.dup(parent)
            self._aggregate_dev=os.fstat(self.fd).st_dev
            for row in rows:
                self._aggregate_time();parts=row['path'].split('/');directory=parent;prefix=''
                for part in parts[:-1]:
                    prefix+=part+'/'
                    directory=self._aggregate_dirs.get('relative:'+prefix) or self._aggregate_directory(directory,part,'relative:'+prefix)
                name=parts[-1];self._aggregate_exact_name(directory,name)
                before=os.stat(name,dir_fd=directory,follow_symlinks=False)
                self._aggregate_regular(before)
                self._aggregate_device(before)
                bound=limits[{'image':'image_bytes','record':'record_bytes','runtime-lock':'lock_bytes'}[row['kind']]]
                if before.st_size>bound:raise Refusal('raw_limit')
                self._aggregate_total+=before.st_size
                if self._aggregate_total>limits['aggregate_bytes']:raise Refusal('raw_limit')
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
                self._aggregate_files[row['path']]=(fd,directory,name,before,bound,None)
            self._aggregate_inventory()
            self._aggregate_check()
        except BaseException as exc:
            self.close()
            if isinstance(exc,Refusal):raise
            if isinstance(exc,FileNotFoundError):raise Refusal('required_binding_gap') from None
            if isinstance(exc,OSError):raise Refusal('member_path_alias') from None
            raise

    def _aggregate_inventory(self):
        expected=self._aggregate_expected
        prefixes={p[:i] for p in expected for i,char in enumerate(p) if char=='/'}
        def visit(fd,prefix):
            for entry in os.scandir(fd):
                self._aggregate_time()
                path=prefix+entry.name
                info=entry.stat(follow_symlinks=False)
                self._aggregate_path(path+'/placeholder' if stat.S_ISDIR(info.st_mode) else path)
                if stat.S_ISDIR(info.st_mode):
                    if path not in prefixes:raise Refusal('aggregate_pair_mismatch')
                    child=self._aggregate_dirs['relative:'+path+'/']
                    visit(child,path+'/')
                else:
                    self._aggregate_regular(info)
                    if path not in expected:raise Refusal('aggregate_pair_mismatch')
        visit(self.fd,'')

    def _aggregate_check(self,path=None):
        self._aggregate_time()
        try:
            for parent,name,fd,dev,ino in self._aggregate_links:
                named=os.stat(name,dir_fd=parent,follow_symlinks=False);held=os.fstat(fd)
                if (not stat.S_ISDIR(named.st_mode) or (named.st_dev,named.st_ino)!=(dev,ino)
                        or (held.st_dev,held.st_ino)!=(dev,ino)):raise Refusal('member_custody_changed')
            subjects=self._aggregate_files.values() if path is None else (self._aggregate_files[path],)
            for fd,parent,name,before,bound,digest in subjects:
                named=os.stat(name,dir_fd=parent,follow_symlinks=False);held=os.fstat(fd)
                if (self._aggregate_identity(named)!=self._aggregate_identity(before)
                        or self._aggregate_identity(held)!=self._aggregate_identity(before)):
                    raise Refusal('member_custody_changed')
        except OSError:raise Refusal('member_custody_changed') from None

    def aggregate_bytes(self,path):
        self._aggregate_path(path)
        if path not in self._aggregate_files:raise Refusal('member_not_in_aggregate')
        self._aggregate_check(path)
        fd,parent,name,before,bound,old_digest=self._aggregate_files[path]
        os.lseek(fd,0,os.SEEK_SET);chunks=[];size=0;digest=hashlib.sha256()
        while True:
            self._aggregate_time()
            raw=os.read(fd,min(65536,bound+1-size))
            if not raw:break
            size+=len(raw)
            if size>bound:raise Refusal('raw_limit')
            chunks.append(raw);digest.update(raw)
        self._aggregate_check(path)
        if size!=before.st_size:raise Refusal('member_custody_changed')
        observed=digest.hexdigest()
        if old_digest is not None and observed!=old_digest:raise Refusal('member_custody_changed')
        self._aggregate_files[path]=(fd,parent,name,before,bound,observed)
        return b''.join(chunks)

    def aggregate_finish(self):
        """Rehash original held bodies and named closure before returning bytes."""
        self._aggregate_check();self._aggregate_inventory()
        for path in self._aggregate_files:self.aggregate_bytes(path)
        self._aggregate_check()

    def __call__(self, path):
        fd, name = self._parent(path)
        try:
            before = os.stat(name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISLNK(before.st_mode):
                target = os.readlink(name, dir_fd=fd)
                relative_path(target)
                return FileObservation(target.encode('utf-8'), 'symlink', stat.S_IMODE(before.st_mode), target)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > self.limit:
                raise ValueError('nonregular_alias_or_size')
            file_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            with os.fdopen(file_fd, 'rb') as stream:
                actual = os.fstat(stream.fileno())
                if (actual.st_dev, actual.st_ino) != (before.st_dev, before.st_ino):
                    raise ValueError('file_replaced')
                raw = stream.read(self.limit + 1)
                after = os.fstat(stream.fileno())
                if (actual.st_size, actual.st_mtime_ns, actual.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ValueError('file_changed')
            if len(raw) > self.limit:
                raise ValueError('file_size_bound')
            return FileObservation(raw, 'regular', stat.S_IMODE(actual.st_mode))
        finally:
            os.close(fd)

    def bytes(self, path):
        observed = self(path)
        if observed.kind != 'regular':
            raise ValueError('artifact_symlink')
        return observed.data

    def inventory(self, context_root):
        relative_path(context_root)
        fd, name = self._parent(context_root + '/placeholder')
        try:
            result = []
            def visit(directory_fd, prefix):
                for entry in sorted(os.scandir(directory_fd), key=lambda e:e.name):
                    path = prefix + '/' + entry.name
                    relative_path(path)
                    info = entry.stat(follow_symlinks=False)
                    if stat.S_ISDIR(info.st_mode):
                        child = os.open(entry.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory_fd)
                        try:
                            visit(child, path)
                        finally:
                            os.close(child)
                    elif stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode):
                        result.append(path)
                        if len(result) > 50000:
                            raise ValueError('inventory_count')
                    else:
                        raise ValueError('inventory_nonregular')
            visit(fd, context_root)
            return result
        finally:
            os.close(fd)
