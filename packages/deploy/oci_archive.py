"""Stream-check OCI platform archives; never extract or acquire dependencies."""
import hashlib
import json
from pathlib import PurePosixPath
import re
import tarfile


def verify(path, source_revision):
    with tarfile.open(path, 'r:*') as archive:
        members = {}
        for item in archive:
            name = item.name.rstrip('/')
            if (name in members or name.startswith('/') or '..' in PurePosixPath(name).parts
                    or '\\' in name or not (item.isfile() or item.isdir())):
                raise ValueError('unsafe or duplicate OCI archive member')
            members[name] = item
        def read(name, limit=1024*1024):
            item = members.get(name)
            if item is None or not item.isfile() or item.size > limit:
                raise ValueError('missing/oversized OCI metadata')
            with archive.extractfile(item) as stream:
                raw = stream.read(limit+1)
            if len(raw) != item.size or len(raw) > limit:
                raise ValueError('incomplete OCI metadata')
            return raw
        if json.loads(read('oci-layout')) != {'imageLayoutVersion':'1.0.0'}:
            raise ValueError('invalid OCI layout')
        def descriptor(entry, *, metadata=False):
            if not isinstance(entry, dict) or re.fullmatch('sha256:[0-9a-f]{64}', str(entry.get('digest'))) is None:
                raise ValueError('invalid OCI digest')
            name = 'blobs/sha256/'+entry['digest'][7:]
            item = members.get(name)
            if (item is None or not item.isfile() or type(entry.get('size')) is not int
                    or item.size != entry['size'] or item.size > 32*1024**3):
                raise ValueError('invalid OCI blob size/type')
            digest = hashlib.sha256()
            with archive.extractfile(item) as stream:
                for chunk in iter(lambda:stream.read(1024*1024), b''):
                    digest.update(chunk)
            if 'sha256:'+digest.hexdigest() != entry['digest']:
                raise ValueError('OCI blob checksum mismatch')
            return json.loads(read(name)) if metadata else None
        index = json.loads(read('index.json'))
        if type(index.get('schemaVersion')) is not int or index['schemaVersion'] != 2 or len(index.get('manifests', [])) != 1:
            raise ValueError('one platform image is required')
        chosen = index['manifests'][0]
        manifest = descriptor(chosen, metadata=True)
        if (manifest.get('mediaType') != 'application/vnd.oci.image.manifest.v1+json'
                or type(manifest.get('schemaVersion')) is not int or manifest['schemaVersion'] != 2
                or not isinstance(manifest.get('layers'), list)):
            raise ValueError('platform manifest required; nested indexes refuse')
        config = descriptor(manifest.get('config'), metadata=True)
        for layer in manifest['layers']:
            descriptor(layer)
        labels = config.get('config', {}).get('Labels') or {}
        if (config.get('os') != 'linux' or config.get('architecture') != 'amd64'
                or labels.get('org.opencontainers.image.source') != 'https://github.com/Kaidera-AI/cortex'
                or labels.get('org.opencontainers.image.version') != '0.1.003-manual.1'
                or labels.get('org.opencontainers.image.revision') != source_revision):
            raise ValueError('native image/source identity mismatch')
        return {'manifest_digest':chosen['digest'], 'platform':'linux/amd64',
                'source_revision':source_revision,'layers_verified':len(manifest['layers'])}
