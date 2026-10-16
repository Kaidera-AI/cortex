"""Acquire the ratified R1 originals; does not grant R01 admission or execute them."""
import argparse
import hashlib
import json
from pathlib import Path
import ssl
import urllib.request


def fetch(url, destination, *, digest=None, size=None, limit=40_000_000):
    request = urllib.request.Request(url, headers={'User-Agent': 'cortex-r02-materials/1'})
    context = ssl.create_default_context()
    with urllib.request.urlopen(request, timeout=45, context=context) as response:
        raw = response.read(limit + 1)
        if len(raw) > limit:
            raise ValueError('material exceeds acquisition limit')
    observed = hashlib.sha256(raw).hexdigest()
    if digest is not None and observed != digest:
        raise ValueError('original material hash mismatch: ' + url)
    if size is not None and len(raw) != size:
        raise ValueError('original material size mismatch: ' + url)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open('xb') as stream:
        stream.write(raw)
    destination.chmod(0o644)
    return {'path': str(destination), 'url': url, 'sha256': observed, 'bytes': len(raw)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    origins = json.loads(Path(__file__).with_name('origins.json').read_bytes())
    args.output.mkdir(parents=True, exist_ok=False)
    records = []
    metadata = {}
    for wheel in origins['wheels']:
        url = wheel['metadata_url']
        if url not in metadata:
            record = fetch(url, args.output / 'metadata' / (wheel['name'] + '.json'), limit=2_000_000)
            metadata[url] = json.loads(Path(record['path']).read_bytes())
            records.append(record)
        candidates = [entry for entry in metadata[url]['urls']
                      if entry['filename'] == wheel['filename']
                      and entry['digests']['sha256'] == wheel['sha256']
                      and entry['size'] == wheel['bytes'] and not entry['yanked']]
        if len(candidates) != 1 or not candidates[0]['url'].startswith('https://files.pythonhosted.org/'):
            raise ValueError('pinned wheel absent or changed: ' + wheel['filename'])
        record = fetch(candidates[0]['url'], args.output / wheel['platform'].replace('/', '-') / wheel['filename'],
                       digest=wheel['sha256'], size=wheel['bytes'])
        records.append(record)
        print(wheel['platform'], wheel['filename'], record['sha256'], flush=True)
    vector = origins['pgvector']
    records.append(fetch(vector['url'], args.output / ('pgvector-' + vector['commit'] + '.tar.gz'),
                         digest=vector['sha256'], size=vector['bytes']))
    (args.output / 'ACQUIRED.json').write_text(json.dumps(records, indent=2, sort_keys=True) + '\n')


if __name__ == '__main__':
    main()
