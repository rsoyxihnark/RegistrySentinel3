import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import registry_sentinel as sentinel


class ScanGroupingTest(unittest.TestCase):
    def parse(self, *lines):
        return sentinel.RegistryCommandParser()._parse_stream(lines).entries

    def scan_recording_opens(self, entries, record):
        def denied(hive, path, access, label):
            record.append((path.casefold(), access))
            return sentinel.OpenKeyResult(None, None, label, access)

        with mock.patch.object(sentinel, "_open_key_in_view", denied):
            return sentinel.RegistryInspector().scan(entries)

    def test_one_open_for_every_spelling_of_the_same_key(self):
        entries = self.parse(
            r"reg add HKLM\Software\Foo /v A /t REG_SZ /d 1 /f",
            r"reg add HKLM\SOFTWARE\FOO /v B /t REG_SZ /d 2 /f",
            r"reg add HKLM\Software\Foo /v C /t REG_SZ /d 3 /reg:64 /f",
        )
        opened = []
        results = self.scan_recording_opens(entries, opened)
        self.assertEqual(len(opened), 1)
        self.assertEqual(len(results), 3)
        self.assertTrue(all(result.access_denied for result in results))

    def test_distinct_keys_and_views_are_opened_separately(self):
        entries = self.parse(
            r"reg add HKLM\Software\Foo /v A /t REG_SZ /d 1 /f",
            r"reg add HKLM\Software\Bar /v B /t REG_SZ /d 2 /f",
            r"reg add HKLM\Software\Foo /v C /t REG_SZ /d 3 /reg:32 /f",
        )
        opened = []
        self.scan_recording_opens(entries, opened)
        self.assertEqual(len(opened), 3)
        self.assertEqual(len(set(opened)), 3)


if __name__ == "__main__":
    unittest.main()
