"""H-D449 native parser/clock gate; tiny fixtures, never product qualification.

Consume every complete make_plan argv unchanged against mirrored fixture paths.
FROM scratch + COPY needs no network, credential, container run or model input.
"""
import argparse
import datetime
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tarfile
import time

EPOCH = 1791586380
ENGINE = '/usr/bin/podman'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load_producer(directory):
    sys.path.insert(0, str(directory))
    spec = importlib.util.spec_from_file_location('native_preflight_producer', directory/'build-manual-linux.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def canonical_argv(argv, root):
    prefix = str(root)
    return [('$FIXTURE_ROOT'+arg[len(prefix):]) if arg == prefix or arg.startswith(prefix+'/') else arg
            for arg in argv]


def verify_receipt(path, producer_dir, source_sha, instance_id, guard_sha, engine_hash=None, production_root=None):
    """Called by the controller's real-build gate; never accept a baseline RED."""
    receipt = json.loads(path.read_bytes())
    if (receipt.get('schema') != 'cortex.native-argv-preflight.v1' or
            receipt.get('result') != 'PASS' or receipt.get('expected_exit') != 0 or
            receipt.get('cleanup_verified') is not True or receipt.get('engine_version') != 'podman version 5.8.2' or
            receipt.get('platform') != 'linux-x86_64' or receipt.get('uid') != 1000 or
            receipt.get('instance_id') != instance_id or receipt.get('guard_receipt_sha256') != guard_sha or
            receipt.get('source_revision') != source_sha or
            receipt.get('producer_sha256') != digest(producer_dir/'build-manual-linux.py') or
            receipt.get('export_writer_sha256') != digest(producer_dir/'deterministic_oci_export.py') or
            receipt.get('driver_sha256') != digest(Path(__file__))):
        raise ValueError('native argv preflight missing, stale, RED or wrong host/source/controller')
    if engine_hash is not None and receipt.get('engine_sha256') != engine_hash:
        raise ValueError('native argv preflight engine bytes differ')
    producer = load_producer(producer_dir)
    root = Path(receipt['fixture_root'])
    expected = producer.make_plan(root, source_sha)['images']
    if production_root is not None:
        real = producer.make_plan(production_root, source_sha)['images']
        if any(canonical_argv(a['argv'], root) != canonical_argv(b['argv'], production_root)
               for a, b in zip(expected, real)):
            raise ValueError('fixture argv differs from complete production argv')
    actual = receipt.get('builds', [])
    if len(actual) != 7 or [row.get('role') for row in actual] != [row['role'] for row in expected]:
        raise ValueError('complete seven-role native argv preflight required')
    for plan, row in zip(expected, actual):
        if (row.get('producer_argv') != plan['argv'] or row.get('exit') != 0 or
                row.get('fixed_image_and_layer_clock') is not True or
                row.get('double_export_byte_equal') is not True or
                row.get('fixture_recipe_sha256') != hashlib.sha256(b'FROM scratch\nCOPY preflight-input /preflight-input\n').hexdigest()):
            raise ValueError('native complete argv/fixture/clock proof differs')
        command = row.get('command', [])
        if command[:5] != [ENGINE, '--root', str(root.parent/'store'), '--runroot', str(root.parent/'runroot')] or command[5:] != plan['argv']:
            raise ValueError('native parser preflight command was modified')
        exports = row.get('export_receipts', [])
        if (len(exports) != 2 or any(x.get('result') != 'PASS' or x.get('format') != 'USTAR' or
                x.get('epoch') != EPOCH for x in exports) or
                (exports[0].get('archive_sha256'), exports[0].get('archive_size')) !=
                (exports[1].get('archive_sha256'), exports[1].get('archive_size')) or
                exports[0].get('archive_sha256') != row.get('archive_sha256') or
                exports[0].get('archive_size') != row.get('archive_size')):
            raise ValueError('native double export verification missing or differs')
    return receipt


def inspect_archive(archive):
    """Check real output clocks and COPY bytes, including every raw blob digest."""
    with tarfile.open(archive, 'r:') as outer:
        for member in outer.getmembers():
            if member.isfile() and member.name.startswith('blobs/sha256/'):
                with outer.extractfile(member) as stream:
                    if hashlib.file_digest(stream, 'sha256').hexdigest() != member.name.rsplit('/', 1)[1]:
                        raise ValueError('preflight OCI blob digest differs')
        index = json.load(outer.extractfile('index.json'))
        manifest = json.load(outer.extractfile('blobs/sha256/'+index['manifests'][0]['digest'].split(':')[1]))
        config = json.load(outer.extractfile('blobs/sha256/'+manifest['config']['digest'].split(':')[1]))
        def epoch(value):
            return datetime.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
        if epoch(config['created']) != EPOCH or any(epoch(row['created']) != EPOCH for row in config['history']):
            raise ValueError('preflight actual image/history clock differs')
        found = []
        for descriptor in manifest['layers']:
            with outer.extractfile('blobs/sha256/'+descriptor['digest'].split(':')[1]) as stream:
                with tarfile.open(fileobj=stream, mode='r|*') as layer:
                    for member in layer:
                        if member.name.lstrip('./') == 'preflight-input':
                            if not member.isfile() or member.mtime != EPOCH or member.mode != 0o644:
                                raise ValueError('preflight COPY type/mode/clock differs')
                            if layer.extractfile(member).read() != b'H-D449 native argv fixture\n':
                                raise ValueError('preflight COPY bytes differ')
                            found.append(member.name)
        if len(found) != 1:
            raise ValueError('one actual COPY member required')
    return True


def execute(options):
    if sys.platform != 'linux' or os.uname().machine != 'x86_64' or os.getuid() != 1000:
        raise ValueError('native unprivileged Linux x86_64 required')
    work = options.work_dir
    if not work.is_absolute() or work.exists() or work.is_symlink() or work.parent.is_symlink():
        raise ValueError('new physical private workdir required')
    if options.output.exists() or options.output.is_symlink():
        raise ValueError('new output receipt required')
    for ancestor in work.parents:
        info = ancestor.lstat()
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_mode & 0o022:
            raise ValueError('writable/symlink staging ancestor refused')
    work.mkdir(mode=0o700)
    if stat.S_IMODE(work.stat().st_mode) != 0o700 or work.stat().st_uid != os.getuid():
        raise ValueError('private owned workdir required')
    producer = load_producer(options.producer_dir)
    root = work/'fixture-root'
    root.mkdir(mode=0o755)
    recipe = b'FROM scratch\nCOPY preflight-input /preflight-input\n'
    for context, name in producer.ROLES.values():
        directory = root/context
        directory.mkdir(parents=True, exist_ok=True)
        file = directory/name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(recipe)
        (directory/'preflight-input').write_bytes(b'H-D449 native argv fixture\n')
        (directory/'preflight-input').chmod(0o644)
        os.utime(directory/'preflight-input', (EPOCH, EPOCH))
    (work/'auth.json').write_text('{}\n')
    (work/'auth.json').chmod(0o600)
    environment = {key: os.environ[key] for key in ['HOME', 'PATH', 'XDG_RUNTIME_DIR'] if key in os.environ}
    environment.update(REGISTRY_AUTH_FILE=str(work/'auth.json'), LANG='C.UTF-8')
    engine = [ENGINE, '--root', str(work/'store'), '--runroot', str(work/'runroot')]
    version = subprocess.check_output([ENGINE, '--version'], text=True, env=environment, timeout=30).strip()
    if version != 'podman version 5.8.2':
        raise ValueError('admitted engine 5.8.2 required')
    def query(args):
        return json.loads(subprocess.check_output(engine+args, text=True, env=environment, timeout=60))
    if query(['images', '--format', 'json']) or query(['ps', '-a', '--external', '--format', 'json']):
        raise ValueError('preflight store not empty; refuse foreign cleanup')
    info = query(['info', '--format', 'json'])
    if info['store']['graphRoot'] != str(work/'store') or info['store']['runRoot'] != str(work/'runroot'):
        raise ValueError('preflight storage binding differs')
    receipt = dict(schema='cortex.native-argv-preflight.v1', instance_id=options.instance_id,
        guard_receipt_sha256=options.guard_receipt_sha256, source_revision=options.source_sha,
        producer_sha256=digest(options.producer_dir/'build-manual-linux.py'), driver_sha256=digest(Path(__file__)),
        engine_version=version, engine_sha256=digest(Path(ENGINE)), platform='linux-x86_64', uid=os.getuid(),
        fixture_root=str(root), expected_exit=options.expected_exit, qualification=False,
        product_build=False, initially_empty_store=True, builds=[], cleanup_verified=False, result='RED')
    writer = options.producer_dir/'deterministic_oci_export.py'
    receipt['export_writer_sha256'] = digest(writer) if writer.exists() else None
    plan = producer.make_plan(root, options.source_sha)
    receipt['complete_plan'] = plan
    failure = None
    try:
        for row in plan['images']:
            label = row['role']
            with (work/(label+'.stdout')).open('xb') as out, (work/(label+'.stderr')).open('xb') as err:
                result = subprocess.run(engine+row['argv'], env=environment, stdout=out, stderr=err, timeout=180)
            record = dict(role=label, producer_argv=row['argv'], command=engine+row['argv'], exit=result.returncode,
                stdout_sha256=digest(work/(label+'.stdout')), stderr_sha256=digest(work/(label+'.stderr')),
                fixture_recipe_sha256=digest(Path(row['argv'][row['argv'].index('--file')+1])), fixed_image_and_layer_clock=False)
            receipt['builds'].append(record)
            if result.returncode != options.expected_exit:
                raise ValueError('native argv preflight unexpected exit for '+label)
            if options.expected_exit == 125:
                text = (work/(label+'.stderr')).read_text()+(work/(label+'.stdout')).read_text()
                if 'timestamp and source-date-epoch would be ambiguous if allowed together' not in text:
                    raise ValueError('baseline RED not expected ambiguity')
            else:
                archive = work/(label+'.oci.tar')
                first = producer.export_image(engine, row['tag'], archive, environment)
                time.sleep(1.1)
                second_archive = work/(label+'.repeat.oci.tar')
                second = producer.export_image(engine, row['tag'], second_archive, environment)
                record['double_export_byte_equal'] = (first['archive_size'], first['archive_sha256']) == (second['archive_size'], second['archive_sha256'])
                if not record['double_export_byte_equal']:
                    raise ValueError('native double export raw bytes differ')
                record['export_receipts'] = [first, second]
                record['fixed_image_and_layer_clock'] = inspect_archive(archive)
                record['archive_sha256'] = digest(archive)
                record['archive_size'] = archive.stat().st_size
        receipt['result'] = 'PASS' if options.expected_exit == 0 else 'EXPECTED_RED'
    except Exception as error:
        failure = error
        receipt['failure'] = type(error).__name__+': '+str(error)
    finally:
        try:
            if query(['ps', '-a', '--external', '--format', 'json']):
                raise ValueError('unexpected preflight container; no foreign cleanup')
            ids = sorted({row['Id'] for row in query(['images', '--format', 'json'])})
            if ids:
                subprocess.run(engine+['image', 'rm', '--force', *ids], check=True, env=environment, timeout=60,
                               capture_output=True)
            receipt['cleanup_verified'] = not query(['images', '--format', 'json']) and not query(['ps', '-a', '--external', '--format', 'json'])
            if not receipt['cleanup_verified']:
                raise ValueError('preflight cleanup not verified')
        except Exception as error:
            failure = failure or error
            receipt['cleanup_failure'] = type(error).__name__+': '+str(error)
            receipt['result'] = 'RED'
        receipt['finished_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with options.output.open('x') as stream:
            json.dump(receipt, stream, indent=2); stream.write('\n')
    if failure:
        raise failure
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['producer-dir', 'work-dir', 'output']:
        parser.add_argument('--'+name, type=Path, required=True)
    for name in ['source-sha', 'instance-id', 'guard-receipt-sha256']:
        parser.add_argument('--'+name, required=True)
    parser.add_argument('--expected-exit', type=int, choices=[0, 125], required=True)
    receipt = execute(parser.parse_args())
    print(json.dumps({key: receipt[key] for key in ['result', 'expected_exit', 'engine_version', 'cleanup_verified']}))


if __name__ == '__main__':
    main()
