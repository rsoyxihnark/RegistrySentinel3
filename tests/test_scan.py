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


class FakeKey:
    def __init__(self, values=(), subkeys=None):
        self.values = list(values)
        self.subkeys = dict(subkeys or {})

    def Close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def enum_value(handle, index):
    if index >= len(handle.values):
        raise OSError("no more values")
    return (handle.values[index], "", 1)


def enum_key(handle, index):
    names = list(handle.subkeys)
    if index >= len(names):
        raise OSError("no more keys")
    return names[index]


def open_child(handle, name, reserved, access):
    return handle.subkeys[name]


class ResetScanTest(unittest.TestCase):
    LINES = (
        r"reg delete HKLM\Software\Demo /f",
        r"reg add HKLM\Software\Demo /v Keep /t REG_SZ /d 1 /f",
        r"reg add HKLM\Software\Demo\Sub /v Inner /t REG_SZ /d 2 /f",
    )

    def scan_against(self, root):
        entries = sentinel.RegistryCommandParser()._parse_stream(self.LINES).entries
        opened = sentinel.OpenKeyResult(root, sentinel.DEFAULT_VIEW_LABEL, None, 0)
        with mock.patch.object(sentinel, "_open_key_in_view", lambda *a: opened), \
                mock.patch.object(sentinel.winreg, "EnumValue", enum_value), \
                mock.patch.object(sentinel.winreg, "EnumKey", enum_key), \
                mock.patch.object(sentinel.winreg, "OpenKey", open_child):
            results = sentinel.RegistryInspector().scan(entries)
        by_line = {e.unique_id: e.source_line for e in entries}
        return next(r for r in results if by_line[r.entry_id] == 1)

    def test_reset_is_compliant_when_the_key_holds_only_listed_entries(self):
        root = FakeKey(["Keep"], {"Sub": FakeKey(["Inner"])})
        self.assertIs(self.scan_against(root).compliant, True)

    def test_reset_is_not_compliant_with_an_unlisted_value(self):
        root = FakeKey(["Keep", "Stray"], {"Sub": FakeKey(["Inner"])})
        result = self.scan_against(root)
        self.assertIs(result.compliant, False)
        self.assertIn("Stray", result.detail)

    def test_reset_is_not_compliant_with_an_unlisted_subkey(self):
        root = FakeKey(["Keep"], {"Sub": FakeKey(["Inner"]), "Extra": FakeKey()})
        result = self.scan_against(root)
        self.assertIs(result.compliant, False)
        self.assertIn("Extra", result.detail)

    def test_reset_is_not_compliant_with_an_unlisted_value_in_a_listed_subkey(self):
        root = FakeKey(["Keep"], {"Sub": FakeKey(["Inner", "Stray"])})
        result = self.scan_against(root)
        self.assertIs(result.compliant, False)
        self.assertIn("Stray", result.detail)


class HiveRefreshTest(unittest.TestCase):
    def test_each_scan_reads_the_loaded_user_hives_again(self):
        entries = sentinel.RegistryCommandParser()._parse_stream(
            [r"reg add HKU\S-1-5-99\Software\Demo /v A /t REG_SZ /d 1 /f"]
        ).entries
        missing = sentinel.OpenKeyResult(None, None, None, 0)
        enumerations = []

        def hku_root(hive, path, reserved, access):
            enumerations.append(hive)
            return FakeKey()

        with mock.patch.object(sentinel, "_open_key_in_view", lambda *a: missing), \
                mock.patch.object(sentinel.winreg, "OpenKey", hku_root), \
                mock.patch.object(sentinel.winreg, "EnumKey", enum_key):
            inspector = sentinel.RegistryInspector()
            inspector.scan(entries)
            inspector.scan(entries)

        self.assertEqual(len(enumerations), 2)


if __name__ == "__main__":
    unittest.main()
