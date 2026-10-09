"""The versioned manifest owns schema locations; filenames are not API identity."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from test_contracts import NEXT, fixture
from cortex_core.contracts import ContractError, validate_event, validate_wire


class SchemaIndexContract(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.contracts = Path(self.directory.name) / "contracts"
        shutil.copytree(NEXT / "contracts", self.contracts)
        self.manifest = json.loads((self.contracts / "provenance.json").read_text())
        self.manifest.update(schema_index_version=1, schemas={
            "outbox_event": {"1": {"file": "outbox-event.schema.json", "schema_id": "urn:kaidera:cortex:outbox:event:1"}},
            "proposed_wire": {"file": "openapi.json"},
        })
        self.addCleanup(patch.stopall)
        patch("cortex_core.contracts.CONTRACTS", self.contracts).start()

    def write_manifest(self):
        (self.contracts / "provenance.json").write_text(json.dumps(self.manifest))

    def test_manifest_routes_renamed_schema_files(self):
        for family, original, renamed in [("outbox_event", "outbox-event.schema.json", "event-v1.json"), ("proposed_wire", "openapi.json", "wire-v1.json")]:
            entry = self.manifest["schemas"][family]
            if family == "outbox_event":
                entry = entry["1"]
            entry["file"] = renamed
            (self.contracts / original).rename(self.contracts / renamed)
        self.write_manifest()
        event = fixture("event-upsert.json")
        self.assertEqual(validate_event(event), event)
        receipt = fixture("write-committed.json")
        self.assertEqual(validate_wire("ProposedWriteReceipt", receipt), receipt)

    def test_unknown_index_version_refused(self):
        self.manifest["schema_index_version"] = 2
        self.write_manifest()
        with self.assertRaises(ContractError):
            validate_event(fixture("event-upsert.json"))

    def test_event_schema_identity_mismatch_refused(self):
        self.manifest["schemas"]["outbox_event"]["1"]["schema_id"] = "urn:other:1"
        self.write_manifest()
        with self.assertRaises(ContractError):
            validate_event(fixture("event-upsert.json"))
