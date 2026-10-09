"""RED-first regression: reject normalized invalid timezone offsets."""
import unittest

from test_contracts import ContractError, fixture, validate_event


class TimestampContract(unittest.TestCase):
    def test_invalid_offsets_are_refused(self):
        for offset in ["+01:60", "-01:60", "+00:99", "+24:00", "+99:00"]:
            value = fixture("event-upsert.json")
            value["occurred_at"] = "2026-10-09T19:06:00" + offset
            with self.subTest(offset=offset), self.assertRaises(ContractError):
                validate_event(value)

    def test_valid_offsets_and_fractional_seconds(self):
        for value in ["2026-10-09T19:06:00Z", "2026-10-09T19:06:00+02:00", "2026-10-09T19:06:00.123-03:30"]:
            event = fixture("event-upsert.json")
            event["occurred_at"] = value
            self.assertEqual(validate_event(event), event)


if __name__ == "__main__":
    unittest.main()
