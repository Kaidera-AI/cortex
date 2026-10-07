"""Public observed DB versions for an owned native CI rehearsal only."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cortex_v2.clients.native_prerequisite import physical_path

MESSAGE = 'native database version missing or mismatched'
SQL = "BEGIN READ ONLY; SELECT json_build_object('server_version', current_setting('server_version'), 'server_version_num', current_setting('server_version_num'), 'vector', (SELECT extversion FROM pg_extension WHERE extname = 'vector')); COMMIT;"


def observe(engine, record, entries, *, target, source_root, source_sha, destination):
    created = False
    try:
        if target not in ('macos-arm64', 'linux-x86_64') or re.fullmatch(r'[a-f0-9]{40}', source_sha) is None:
            raise ValueError
        image = entries['db']['config_id']
        if not isinstance(image, str) or re.fullmatch(r'sha256:[a-f0-9]{64}', image) is None:
            raise ValueError
        recipe = Path(source_root) / 'deploy/release' / ('Containerfile.db.linux-amd64' if target == 'linux-x86_64' else 'Containerfile.db')
        physical_path(recipe)
        first = recipe.read_text().splitlines()[0]
        match = re.fullmatch(r'FROM docker.io/pgvector/pgvector:([0-9]+\.[0-9]+\.[0-9]+)-pg([0-9]+)@(sha256:[a-f0-9]{64})', first)
        if match is None:
            raise ValueError
        vector, major, digest = match.groups()
        from install_candidate import namespace
        raw = engine.run(['exec', namespace(record) + '_db', 'psql', '--no-psqlrc',
            '--username=cortex_v2_owner', '--dbname=cortex_v2', '-qAt', '-v', 'ON_ERROR_STOP=1', '-c', SQL], read=True)
        from linux_build_catalog import strict_json
        value = strict_json(raw.encode(), limit=4096)
        if (set(value) != {'server_version', 'server_version_num', 'vector'}
                or value['vector'] != vector or not isinstance(value['server_version'], str)
                or re.fullmatch(re.escape(major) + r'\.[0-9]+(?: \([^\r\n]*\))?', value['server_version']) is None
                or not isinstance(value['server_version_num'], str)
                or re.fullmatch(r'[0-9]{6}', value['server_version_num']) is None
                or int(value['server_version_num']) // 10000 != int(major)):
            raise ValueError
        result = dict(value, schema='cortex.database-version.v1', source_sha=source_sha,
                      target=target, image_config_id=image, base_tag=vector + '-pg' + major,
                      base_digest=digest, status='PASS')
        destination = Path(destination)
        physical_path(destination.parent)
        with destination.open('x') as stream:
            created = True
            json.dump(result, stream, indent=2); stream.write('\n')
            stream.flush(); os.fsync(stream.fileno())
        return result
    except Exception:
        if created:
            try: destination.unlink()
            except OSError: pass
        raise RuntimeError(MESSAGE) from None
