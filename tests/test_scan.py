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
    def __init__(self, values=None, subkeys=None):
        self.values = dict(values or {})
        self.subkeys = dict(subkeys or {})

    def Close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def enum_value(handle, index):
    names = list(handle.values)
    if index >= len(names):
        raise OSError("no more values")
    return (names[index], handle.values[names[index]], sentinel.winreg.REG_SZ)


def enum_key(handle, index):
    names = list(handle.subkeys)
    if index >= len(names):
        raise OSError("no more keys")
    return names[index]


def query_value(handle, name):
    if name not in handle.values:
        raise FileNotFoundError(name)
    return (handle.values[name], sentinel.winreg.REG_SZ)


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

        def open_at(hive, path, access, label):
            node = root
            for part in path.split("\\")[2:]:
                node = node.subkeys[part]
            return sentinel.OpenKeyResult(node, sentinel.DEFAULT_VIEW_LABEL, None, access)

        with mock.patch.object(sentinel, "_open_key_in_view", open_at), \
                mock.patch.object(sentinel.winreg, "EnumValue", enum_value), \
                mock.patch.object(sentinel.winreg, "EnumKey", enum_key), \
                mock.patch.object(sentinel.winreg, "QueryValueEx", query_value), \
                mock.patch.object(sentinel.winreg, "OpenKey", open_child):
            results = sentinel.RegistryInspector().scan(entries)
        by_line = {e.unique_id: e.source_line for e in entries}
        return {by_line[r.entry_id]: r for r in results}

    def test_reset_is_compliant_when_the_key_holds_only_listed_entries(self):
        root = FakeKey({"Keep": "1"}, {"Sub": FakeKey({"Inner": "2"})})
        results = self.scan_against(root)
        self.assertIs(results[1].compliant, True)
        self.assertIs(results[2].compliant, True)
        self.assertIs(results[3].compliant, True)

    def test_reset_is_not_compliant_with_an_unlisted_value(self):
        root = FakeKey({"Keep": "1", "Stray": "x"}, {"Sub": FakeKey({"Inner": "2"})})
        result = self.scan_against(root)[1]
        self.assertIs(result.compliant, False)
        self.assertIn("Stray", result.detail)

    def test_reset_is_not_compliant_with_an_unlisted_subkey(self):
        root = FakeKey({"Keep": "1"}, {"Sub": FakeKey({"Inner": "2"}), "Extra": FakeKey()})
        result = self.scan_against(root)[1]
        self.assertIs(result.compliant, False)
        self.assertIn("Extra", result.detail)

    def test_reset_is_not_compliant_with_an_unlisted_value_in_a_listed_subkey(self):
        root = FakeKey({"Keep": "1"}, {"Sub": FakeKey({"Inner": "2", "Stray": "x"})})
        result = self.scan_against(root)[1]
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
