"""Exact-head D1 contract control for an authorized non-memory C05 record."""

from uuid import UUID

from core_db_fixture import READ_A, WRITE_A, uid
from cortex_core.records import Records
from test_memory_api import MemoryAPI


class GenericRecordContract(MemoryAPI):
    def test_authorized_c05_record_is_readable_by_generic_route(self):
        record_id = UUID(uid(500))
        receipt = Records(self.request, WRITE_A, UUID(uid(1)), UUID(uid(3))).put(
            record_id,
            "knowledge",
            b'{"content":"committed generic D1 record"}',
            0,
            "vera-d1-generic-record",
        )
        authorized = Records(self.request, READ_A, UUID(uid(1)), UUID(uid(3))).get(record_id)
        self.assertIsNotNone(authorized)
        self.assertEqual(authorized.kind, "knowledge")
        self.assertEqual(authorized.payload_sha256, receipt.payload_sha256)
        status, body = self.call("GET", "/records/" + str(record_id), READ_A)
        self.assertEqual(status, 200, "authorized committed C05 record returned 404")
        self.assertEqual(body["revision"], receipt.revision)
        self.assertEqual(body["payload_sha256"], receipt.payload_sha256)
