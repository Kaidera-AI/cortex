"""Common spelling variants of the same unreviewed reflection capability."""
import unittest
import test_reflected_writers as witness


class ReflectionAliases(unittest.TestCase):
    def test_imported_qualified_and_mapping_getattr_aliases_fail_closed(self):
        checker=witness.ReflectedWriters('test_hale_future_reflected_writer_is_unclassified').checker()
        for source in (
            "from builtins import getattr as lookup\ndef writer(receiver,opaque):\n    callback=lookup(receiver,'execute')\n    return callback(opaque)\n",
            "import builtins\ndef writer(receiver,opaque):\n    callback=builtins.getattr(receiver,'execute')\n    return callback(opaque)\n",
            "def writer(receiver,opaque):\n    callback=__builtins__['getattr'](receiver,'execute')\n    return callback(opaque)\n",
        ):
            with self.subTest(source=source):
                self.assertTrue(checker.source_reference_flags(witness.FILE,source))
