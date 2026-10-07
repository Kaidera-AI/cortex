#!/usr/bin/env python3
"""Explicit TEST-only Minisign-format fixture. Never production qualification."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path


def sign_test_manifest(body):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
    key=Ed25519PrivateKey.generate();key_id=os.urandom(8)
    public=base64.b64encode(b'Ed'+key_id+key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
    first=key.sign(hashlib.blake2b(body,digest_size=64).digest())
    comment='TEST ONLY Gate A unsigned-release rehearsal; qualification=false'
    packet=base64.b64encode(b'ED'+key_id+first).decode()
    second=base64.b64encode(key.sign(first+comment.encode())).decode()
    signature='untrusted comment: TEST ONLY Gate A\n'+packet+'\ntrusted comment: '+comment+'\n'+second+'\n'
    # Private key never serialized, retained or returned.
    return {'public_key':public,'signature':signature,'trust':'TEST_ONLY_GATE_A','qualification':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();out=args.output.absolute()
    if out.exists():raise ValueError('TEST fixture output must be new')
    out.mkdir(parents=True,mode=0o700)
    body=args.manifest.read_bytes();fixture=sign_test_manifest(body)
    for name,data in [('release.json',body),('release.json.minisig',fixture['signature'].encode()),('TEST-public-key.txt',fixture['public_key'].encode()+b'\n')]:
        path=out/name;path.write_bytes(data);path.chmod(0o600)
    (out/'TEST-receipt.json').write_text(json.dumps({'trust':fixture['trust'],'qualification':False,'manifest_sha256':hashlib.sha256(body).hexdigest()},indent=2)+'\n')
    print(json.dumps({'fixture':str(out),'trust':fixture['trust'],'qualification':False}))

if __name__=='__main__':main()
