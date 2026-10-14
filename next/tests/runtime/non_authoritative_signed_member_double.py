"""TEST ONLY, NON-AUTHORITATIVE: no signature, root, native or install acceptance.

Only tests patch the port's internal authority call with this in-memory double.
It never constructs a sealed result; the port does that after reading this double.
This file is outside the product import tree and product imports fail the gate.
"""
import inspect
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cortex_core.release.codec import sha256


def reject_product_import():
    frame = inspect.currentframe().f_back
    while frame:
        if '/src/cortex_core/' in str(Path(frame.f_code.co_filename)):
            raise ImportError('non-authoritative test double is forbidden in product code')
        frame = frame.f_back


reject_product_import()


class NonAuthoritativeSignedMemberDouble:
    """Non-authoritative, test-only original-byte delivery; NOT an R01 verifier."""

    def __init__(self, raw, *, aggregate_id='test-only-aggregate', member_name='runtime.lock.json',
                 signing_root_id='test-only-root', declared_sha=None):
        self.raw = raw
        self.aggregate_id = aggregate_id
        self.member_name = member_name
        self.signing_root_id = signing_root_id
        self.declared_sha = declared_sha or sha256(raw)
        self.calls = []
        self.reads = []

    def __call__(self, aggregate_id, member_name):
        self.calls.append((aggregate_id, member_name))
        return SimpleNamespace(aggregate_id=self.aggregate_id, signing_root_id=self.signing_root_id,
                               member_name=self.member_name, member_sha256=self.declared_sha,
                               read_original=self.read_original)

    def read_original(self, limit):
        self.reads.append(limit)
        return self.raw

    def installed(self, port_module):
        return patch.object(port_module, '_original_authority', self)
