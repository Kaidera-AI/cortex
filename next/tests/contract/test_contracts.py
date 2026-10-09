"""C01 I0 behavior checks; unchanged between RED and GREEN."""
import copy
import json
from pathlib import Path
import sys
import unittest

NEXT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(NEXT / "src"))
from cortex_core.contracts import (  # noqa: E402
    ContractError, validate_event, validate_wire, validate_openapi,
)


def fixture(name):
    return json.loads((NEXT / "contracts" / "fixtures" / name).read_text())


class EventContract(unittest.TestCase):
    def test_upsert_and_delete_are_valid(self):
        for name in ["event-upsert.json", "event-delete.json"]:
            value = fixture(name)
            original = copy.deepcopy(value)
            self.assertEqual(validate_event(value), original)
            self.assertEqual(value, original)

    def test_all_required_fields_refuse_omission(self):
        event = fixture("event-upsert.json")
        for key in event:
            with self.subTest(key=key):
                value = dict(event)
                del value[key]
                with self.assertRaises(ContractError):
                    validate_event(value)

    def test_scope_and_object_identifiers_are_canonical_uuids(self):
        for key in ["event_id", "installation_id", "tenant_id", "project_id", "aggregate_id", "payload_ref"]:
            for bad in ["", "other-project", "../secret", None, 7, "00000000000000000000000000000000"]:
                with self.subTest(key=key, bad=bad):
                    value = fixture("event-upsert.json")
                    value[key] = bad
                    with self.assertRaises(ContractError):
                        validate_event(value)

    def test_revision_positive_and_schema_supported(self):
        for key, values in [("aggregate_revision", [0, -1, True, "1", 1.5]), ("schema_version", [0, 2, True, "1"])]:
            for bad in values:
                with self.subTest(key=key, bad=bad):
                    value = fixture("event-upsert.json")
                    value[key] = bad
                    with self.assertRaises(ContractError):
                        validate_event(value)

    def test_delete_and_tombstone_agree_in_both_directions(self):
        for operation, tombstone in [("delete", False), ("upsert", True), ("unknown", False), ("upsert", "false")]:
            value = fixture("event-upsert.json")
            value.update(operation=operation, tombstone=tombstone)
            with self.assertRaises(ContractError):
                validate_event(value)

    def test_unknown_fields_refused(self):
        value = fixture("event-upsert.json")
        value["published_cursor"] = "999"
        with self.assertRaises(ContractError):
            validate_event(value)

    def test_payload_identity_kind_digest_and_timestamp_checked(self):
        for key, bad in [("aggregate_kind", ""), ("aggregate_kind", "shell command!"), ("payload_sha256", "a" * 63), ("payload_sha256", "G" * 64), ("occurred_at", "yesterday")]:
            value = fixture("event-upsert.json")
            value[key] = bad
            with self.subTest(key=key, bad=bad), self.assertRaises(ContractError):
                validate_event(value)

    def test_bad_input_is_contract_error(self):
        for value in [None, [], "event", 5]:
            with self.assertRaises(ContractError):
                validate_event(value)


class WireContract(unittest.TestCase):
    def test_minimal_valid_wire_fixtures(self):
        for schema, name in [("ProposedWriteReceipt", "write-committed.json"), ("ProposedCapabilityState", "capability-ready.json"), ("ProposedError", "capability-unavailable.json"), ("ProposedError", "core-unavailable.json")]:
            value = fixture(name)
            self.assertEqual(validate_wire(schema, value), value)

    def test_queued_delivery_is_not_a_commit_receipt(self):
        value = fixture("write-committed.json")
        value["durability"] = "queued"
        with self.assertRaises(ContractError):
            validate_wire("ProposedWriteReceipt", value)

    def test_write_receipt_revision_bounds_and_projection(self):
        for key, bad in [("aggregate_version", 0), ("event_id", "fake"), ("projection", "complete-ish")]:
            value = fixture("write-committed.json")
            value[key] = bad
            with self.assertRaises(ContractError):
                validate_wire("ProposedWriteReceipt", value)

    def test_capability_state_and_error_codes_are_typed(self):
        for schema, name, key, bad in [("ProposedCapabilityState", "capability-ready.json", "state", "green"), ("ProposedError", "core-unavailable.json", "code", "all good"), ("ProposedError", "core-unavailable.json", "retryable", "yes")]:
            value = fixture(name)
            value[key] = bad
            with self.assertRaises(ContractError):
                validate_wire(schema, value)

    def test_unknown_schema_refused(self):
        with self.assertRaises(ContractError):
            validate_wire("../../../secret", {})


class OpenAPIContract(unittest.TestCase):
    def document(self):
        return json.loads((NEXT / "contracts" / "openapi.json").read_text())

    def test_inventory_and_references(self):
        self.assertEqual(validate_openapi(self.document()), 128)

    def test_missing_operation_refused(self):
        value = self.document()
        del value["paths"]["/memory"]["post"]
        with self.assertRaises(ContractError):
            validate_openapi(value)

    def test_duplicate_operation_refused(self):
        value = self.document()
        value["paths"]["/memory"]["post"]["operationId"] = value["paths"]["/search"]["post"]["operationId"]
        with self.assertRaises(ContractError):
            validate_openapi(value)

    def test_unowned_route_refused(self):
        value = self.document()
        value["paths"]["/memory"]["post"]["x-c01-disposition"] = "ignore"
        with self.assertRaises(ContractError):
            validate_openapi(value)

    def test_broken_reference_refused(self):
        value = self.document()
        value["components"]["schemas"]["broken"] = {"$ref": "#/missing"}
        with self.assertRaises(ContractError):
            validate_openapi(value)

    def test_c02_gaps_cannot_be_removed(self):
        value = self.document()
        del value["paths"]["/memory"]["post"]["x-c02-freeze-gaps"]
        with self.assertRaises(ContractError):
            validate_openapi(value)


if __name__ == "__main__":
    unittest.main()
