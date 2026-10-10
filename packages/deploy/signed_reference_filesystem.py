"""Read-only merged OCI filesystem receipts; never extract or rewrite images."""
import argparse
import hashlib
import json
from pathlib import PurePosixPath
import posixpath
import re
import struct
import tarfile

OLD = '15aff361c9835f24d1e5eedba4be9b5f5168a87f'
NEW = 'db9181a26048dcfdcd03f786a52a08d68a84be9a'


def path_name(value):
    value = value.removeprefix('./').lstrip('/')
    if '..' in PurePosixPath(value).parts or '\\' in value:
        raise ValueError('ambiguous filesystem path')
    return posixpath.normpath(value)


def metadata(outer):
    index = json.load(outer.extractfile('index.json'))
    if len(index['manifests']) != 1:
        raise ValueError('one platform required')
    manifest = json.load(outer.extractfile('blobs/sha256/' + index['manifests'][0]['digest'][7:]))
    config = json.load(outer.extractfile('blobs/sha256/' + manifest['config']['digest'][7:]))
    return manifest, config


def filesystem(archive):
    state = {}
    times = {}
    with tarfile.open(archive, 'r:*') as outer:
        manifest, config = metadata(outer)
        for descriptor in manifest['layers']:
            additions, deletions, opaque = {}, [], []
            with outer.extractfile('blobs/sha256/' + descriptor['digest'][7:]) as stream:
                with tarfile.open(fileobj=stream, mode='r|*') as layer:
                    for item in layer:
                        name = path_name(item.name)
                        base = posixpath.basename(name)
                        parent = posixpath.dirname(name)
                        if base == '.wh..wh..opq':
                            opaque.append(parent)
                            continue
                        if base.startswith('.wh.'):
                            deletions.append(posixpath.join(parent, base[4:]))
                            continue
                        row = {'type': item.type.decode('ascii'), 'mode': item.mode,
                               'uid': item.uid, 'gid': item.gid,
                               'xattrs': {k: v for k, v in item.pax_headers.items()
                                          if 'xattr' in k or k.startswith('SCHILY.acl')}}
                        times[name] = item.pax_headers.get('mtime', item.mtime)
                        if item.isfile():
                            with layer.extractfile(item) as content:
                                row['sha256'] = hashlib.file_digest(content, 'sha256').hexdigest()
                            row['size'] = item.size
                            if name in ('app/release_identity.json', 'etc/cortex/pg_hba.conf', 'etc/cortex/pg_ident.conf'):
                                # Content is retained only for these public identity/auth files.
                                # Stream tar cannot rewind: callers separately retrieve exact bytes.
                                row['public_content'] = True
                        elif item.issym():
                            row['link'] = item.linkname
                        elif item.islnk():
                            row['hardlink'] = path_name(item.linkname)
                        elif item.ischr() or item.isblk():
                            row['device'] = [item.devmajor, item.devminor]
                        elif not (item.isdir() or item.isfifo()):
                            raise ValueError('unsupported filesystem node')
                        if name in additions:
                            raise ValueError('duplicate non-whiteout path within layer')
                        additions[name] = row
            for target in deletions:
                state = {k: v for k, v in state.items() if k != target and not k.startswith(target + '/')}
            for directory in opaque:
                state = {k: v for k, v in state.items()
                         if (k == '.' if directory in ('', '.') else not k.startswith(directory + '/'))}
            for name, row in additions.items():
                if row['type'] != '5':
                    state = {k: v for k, v in state.items() if not k.startswith(name + '/')}
                state[name] = row
            # Resolve hardlinks against the resulting layer, retaining identity explicitly.
            for name, row in additions.items():
                if 'hardlink' in row:
                    target = row['hardlink']
                    seen = {name}
                    while 'hardlink' in state.get(target, {}):
                        if target in seen:
                            raise ValueError('cyclic hardlink')
                        seen.add(target)
                        target = state[target]['hardlink']
                    destination = state.get(target)
                    if destination is None or destination['type'] not in ('0', '\x00'):
                        raise ValueError('unresolved hardlink')
                    row['target_content'] = {k: destination[k] for k in ('sha256', 'size')}
    return state, times, manifest, config


def public_file(archive, wanted):
    found = None
    with tarfile.open(archive, 'r:*') as outer:
        manifest, _ = metadata(outer)
        for descriptor in manifest['layers']:
            with outer.extractfile('blobs/sha256/' + descriptor['digest'][7:]) as stream:
                with tarfile.open(fileobj=stream, mode='r|*') as layer:
                    for item in layer:
                        if path_name(item.name) == wanted:
                            if not item.isfile() or item.size > 65536:
                                raise ValueError('public file type/size mismatch')
                            found = layer.extractfile(item).read()
    if found is None:
        raise ValueError('required public file missing')
    return found


def selected_files(archive, wanted):
    """One streaming pass for changed caches/shadow; return bytes only in memory."""
    found = {}
    with tarfile.open(archive, 'r:*') as outer:
        manifest, _ = metadata(outer)
        for descriptor in manifest['layers']:
            with outer.extractfile('blobs/sha256/' + descriptor['digest'][7:]) as stream:
                with tarfile.open(fileobj=stream, mode='r|*') as layer:
                    for item in layer:
                        name = path_name(item.name)
                        if name not in wanted:
                            continue
                        if not item.isfile() or item.size > 16*1024*1024:
                            raise ValueError('generated-metadata file type/size mismatch')
                        found[name] = layer.extractfile(item).read()
    if set(found) != wanted:
        raise ValueError('generated-metadata file missing')
    return found


def generated_metadata(old, new, before, after, before_times, after_times, changed):
    wanted = {p for p in changed if (p.endswith('.pyc') or p == 'etc/shadow')
              and 'sha256' in before.get(p,{}) and 'sha256' in after.get(p,{})}
    if not wanted:
        return [], []
    old_files, new_files = selected_files(old, wanted), selected_files(new, wanted)
    permitted, receipts = [], []
    for name in sorted(wanted):
        a, b = before.get(name), after.get(name)
        if a is None or b is None or any(a.get(k) != b.get(k) for k in ('type','mode','uid','gid','xattrs')):
            continue
        left, right = old_files[name], new_files[name]
        if name.endswith('.pyc'):
            if '/__pycache__/' in name and '.cpython-' in name:
                source = posixpath.dirname(posixpath.dirname(name)) + '/' + posixpath.basename(name).split('.cpython-', 1)[0] + '.py'
            elif '/__pycache__/' not in name:
                source = name[:-1]
            else:
                continue
            src_old, src_new = before.get(source), after.get(source)
            if src_old is None or src_new is None or src_old != src_new or 'sha256' not in src_old:
                continue
            if len(left) < 16 or len(right) < 16 or left[:8] != right[:8] or left[12:] != right[12:]:
                continue
            magic_a, flags_a, mtime_a, size_a = struct.unpack('<4sIII', left[:16])
            magic_b, flags_b, mtime_b, size_b = struct.unpack('<4sIII', right[:16])
            if (flags_a != 0 or flags_b != 0 or size_a != src_old['size'] or size_b != src_new['size']
                    or mtime_a != int(float(before_times[source])) or mtime_b != int(float(after_times[source]))):
                continue
            permitted.append(name)
            receipts.append({'path':name,'class':'pyc_source_mtime_only', 'source':source,
                             'source_sha256':src_old['sha256'], 'body_sha256':hashlib.sha256(left[16:]).hexdigest(),
                             'magic':magic_a.hex(),'flags':flags_a,'source_size':size_a,
                             'source_mtime_before':mtime_a,'source_mtime_after':mtime_b,
                             'header_matches_actual_source_stats':True,'attributes_equal':True})
        else:
            try:
                old_rows = [row.split(':') for row in left.decode('utf-8').splitlines(keepends=True)]
                new_rows = [row.split(':') for row in right.decode('utf-8').splitlines(keepends=True)]
            except UnicodeError:
                continue
            if len(old_rows) != len(new_rows):
                continue
            names, rows, valid = set(), [], True
            for x,y in zip(old_rows,new_rows):
                if len(x) != 9 or len(y) != 9 or x[0] != y[0] or x[0] in names:
                    valid = False;break
                names.add(x[0]); delta = [i for i in range(9) if x[i] != y[i]]
                if not delta:
                    continue
                if (delta != [2] or not x[1].startswith(('!','*')) or not y[1].startswith(('!','*'))
                        or not re.fullmatch('[0-9]+',x[2]) or not re.fullmatch('[0-9]+',y[2])):
                    valid = False;break
                rows.append({'account':x[0], 'last_change_day_before':x[2], 'last_change_day_after':y[2],
                             'credential_equal_and_locked_both':True,'other_fields_equal':True})
            if valid and rows:
                permitted.append(name)
                receipts.append({'path':name,'class':'locked_account_last_change_day_only',
                                 'changed_accounts':rows,'attributes_equal':True,'credential_contents_exported':False})
    return permitted, receipts


def compare(old, new, role, expected_db=None):
    before, before_times, old_manifest, old_config = filesystem(old)
    after, after_times, new_manifest, new_config = filesystem(new)
    if old_config['config']['Labels']['org.opencontainers.image.revision'] != OLD:
        raise ValueError('old source identity mismatch')
    if new_config['config']['Labels']['org.opencontainers.image.revision'] != NEW:
        raise ValueError('new source identity mismatch')
    old_runtime, new_runtime = json.loads(json.dumps(old_config['config'])), json.loads(json.dumps(new_config['config']))
    for runtime in (old_runtime, new_runtime):
        runtime['Labels'].pop('org.opencontainers.image.revision')
    if old_runtime != new_runtime:
        raise ValueError('runtime config differs beyond revision label')
    changed = [name for name in sorted(set(before) | set(after)) if before.get(name) != after.get(name)]
    permitted = []
    if role == 'api':
        name = 'app/release_identity.json'
        old_identity, new_identity = public_file(old, name), public_file(new, name)
        if old_identity.count(OLD.encode()) != 1 or new_identity != old_identity.replace(OLD.encode(), NEW.encode()):
            raise ValueError('API identity bytes differ beyond the exact revision replacement')
        a, b = json.loads(old_identity), json.loads(new_identity)
        if a.pop('source_revision') != OLD or b.pop('source_revision') != NEW or a != b:
            raise ValueError('API identity differs beyond exact source_revision')
        left, right = dict(before[name]), dict(after[name])
        for row in (left, right):
            row.pop('sha256'); row.pop('size')
        if left != right:
            raise ValueError('API identity mode/owner/type differs')
        permitted = [name]
    elif role == 'db':
        if expected_db is None:
            raise ValueError('DB requires committed auth-config bytes')
        permitted = ['etc/cortex/pg_hba.conf', 'etc/cortex/pg_ident.conf']
        for name in permitted:
            if public_file(new, name) != expected_db[name]:
                raise ValueError('DB auth config differs from reviewed source')
            a, b = dict(before[name]), dict(after[name])
            for row in (a, b):
                row.pop('sha256'); row.pop('size')
            if a != b:
                raise ValueError('DB auth config mode/owner/type differs')
    elif role not in ('tls', 'provider', 'embed-worker', 'graph-worker', 'pdf-worker'):
        raise ValueError('unknown role')
    generated_permitted, generated_receipts = generated_metadata(old, new, before, after, before_times, after_times, changed)
    permitted += generated_permitted
    unexpected = [name for name in changed if name not in permitted]
    return {'role': role, 'result': 'RED' if unexpected else 'PASS',
            'old_source_revision': OLD, 'new_source_revision': NEW,
            'filesystem_paths_before': len(before), 'filesystem_paths_after': len(after),
            'filesystem_layer_digests_equal': old_manifest['layers'] == new_manifest['layers'],
            'changed_paths': changed, 'unexpected_paths': unexpected,
            'generated_metadata_receipt':generated_receipts,
            'receipt_ruling':'kai r426 2026-10-09 13:27 all roles; exact-source/bytecode/stat/locked-account conditions',
            'differences': {name: {'before': before.get(name), 'after': after.get(name)} for name in changed},
            'tar_timestamp_changes': sum(before_times.get(k) != after_times.get(k) for k in set(before) | set(after)),
            'timestamp_policy': 'recorded separately; never ignored for file bytes, type, mode, numeric owner, links or xattrs',
            'qualification': False}


if __name__ == '__main__':
    from pathlib import Path
    p = argparse.ArgumentParser()
    p.add_argument('--old', required=True); p.add_argument('--new', required=True)
    p.add_argument('--role', required=True); p.add_argument('--source'); p.add_argument('--output', required=True)
    args = p.parse_args()
    db = None if args.role != 'db' else {
        'etc/cortex/' + name: (Path(args.source) / 'packages/deploy' / name).read_bytes()
        for name in ('pg_hba.conf', 'pg_ident.conf')}
    result = compare(args.old, args.new, args.role, db)
    with open(args.output, 'x') as stream:
        json.dump(result, stream, indent=2); stream.write('\n')
    print(json.dumps({'role':result['role'],'result':result['result'],
                      'unexpected_path_count':len(result['unexpected_paths']),
                      'unexpected_sample':result['unexpected_paths'][:5],
                      'generated_metadata_paths':len(result['generated_metadata_receipt'])}))
    raise SystemExit(1 if result['result'] == 'RED' else 0)
