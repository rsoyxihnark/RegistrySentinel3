import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import registry_sentinel as sentinel

HKLM = sentinel.winreg.HKEY_LOCAL_MACHINE


class Node:
    def __init__(self):
        self.values = {}
        self.subkeys = {}


class Handle:
    def __init__(self, hive, path, node):
        self.hive = hive
        self.path = path
        self.node = node

    def Close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeRegistry:
    def __init__(self, tree):
        self.hives = {}
        for path, values in tree.items():
            self.key(HKLM, path, create=True).values.update(
                {name: (value, sentinel.winreg.REG_SZ) for name, value in values.items()}
            )

    def key(self, hive, path, create=False):
        node = self.hives.setdefault(hive, Node())
        for part in [p for p in path.split("\\") if p]:
            match = next((n for n in node.subkeys if n.casefold() == part.casefold()), None)
            if match is None:
                if not create:
                    raise FileNotFoundError(path)
                node.subkeys[part] = Node()
                match = part
            node = node.subkeys[match]
        return node

    def snapshot(self, hive=HKLM):
        found = {}

        def walk(node, prefix):
            found[prefix] = {name: value for name, (value, _t) in node.values.items()}
            for name, child in node.subkeys.items():
                walk(child, sentinel._join_sub_path(prefix, name))

        walk(self.hives.setdefault(hive, Node()), "")
        return found

    def open_key(self, hive, path, reserved=0, access=0):
        return Handle(hive, path, self.key(hive, path))

    def create_key(self, hive, path, reserved=0, access=0):
        return Handle(hive, path, self.key(hive, path, create=True))

    def delete_key(self, hive, path, access=0, reserved=0):
        parts = [p for p in path.split("\\") if p]
        parent = self.key(hive, "\\".join(parts[:-1]))
        match = next((n for n in parent.subkeys if n.casefold() == parts[-1].casefold()), None)
        if match is None:
            raise FileNotFoundError(path)
        if parent.subkeys[match].subkeys:
            raise OSError(f"{path} still has subkeys")
        del parent.subkeys[match]

    def patched(self):
        def set_value(handle, name, reserved, value_type, data):
            handle.node.values[name] = (data, value_type)

        def delete_value(handle, name):
            if name not in handle.node.values:
                raise FileNotFoundError(name)
            del handle.node.values[name]

        def enum_key(handle, index):
            names = list(handle.node.subkeys)
            if index >= len(names):
                raise OSError("no more keys")
            return names[index]

        def enum_value(handle, index):
            names = list(handle.node.values)
            if index >= len(names):
                raise OSError("no more values")
            data, value_type = handle.node.values[names[index]]
            return (names[index], data, value_type)

        return mock.patch.multiple(
            sentinel.winreg,
            OpenKey=self.open_key,
            CreateKeyEx=self.create_key,
            DeleteKey=lambda hive, path: self.delete_key(hive, path),
            DeleteKeyEx=self.delete_key,
            SetValueEx=set_value,
            DeleteValue=delete_value,
            EnumKey=enum_key,
            EnumValue=enum_value,
        )


class ApplyTest(unittest.TestCase):
    def apply(self, tree, *lines):
        registry = FakeRegistry(tree)
        entries = sentinel.RegistryCommandParser()._parse_stream(lines).entries
        with registry.patched():
            outcome = sentinel.RegistryApplier().execute(entries)
        return registry, outcome

    def test_a_key_delete_removes_the_whole_branch_and_nothing_beside_it(self):
        registry, outcome = self.apply(
            {
                "Software\\Demo": {"A": "1"},
                "Software\\Demo\\Sub\\Deeper": {"B": "2"},
                "Software\\Keep": {"C": "3"},
            },
            r"reg delete HKLM\Software\Demo /f",
        )
        self.assertEqual((outcome.succeeded, outcome.failed), (1, 0))
        self.assertEqual(
            registry.snapshot(),
            {"": {}, "Software": {}, "Software\\Keep": {"C": "3"}},
        )

    def test_a_reset_leaves_the_key_holding_only_the_listed_values(self):
        registry, outcome = self.apply(
            {
                "Software\\Demo": {"A": "1", "Keep": "old"},
                "Software\\Demo\\Stray": {"B": "2"},
            },
            r"reg delete HKLM\Software\Demo /f",
            r"reg add HKLM\Software\Demo /v Keep /t REG_SZ /d 1 /f",
            r"reg add HKLM\Software\Demo\Sub /v Inner /t REG_SZ /d 2 /f",
        )
        self.assertEqual((outcome.succeeded, outcome.failed), (3, 0))
        self.assertEqual(
            registry.snapshot(),
            {
                "": {},
                "Software": {},
                "Software\\Demo": {"Keep": "1"},
                "Software\\Demo\\Sub": {"Inner": "2"},
            },
        )

    def test_deleting_all_values_leaves_the_subkeys_alone(self):
        registry, outcome = self.apply(
            {"Software\\Demo": {"A": "1", "B": "2"}, "Software\\Demo\\Sub": {"C": "3"}},
            r"reg delete HKLM\Software\Demo /va /f",
        )
        self.assertEqual((outcome.succeeded, outcome.failed), (1, 0))
        self.assertEqual(
            registry.snapshot(),
            {"": {}, "Software": {}, "Software\\Demo": {}, "Software\\Demo\\Sub": {"C": "3"}},
        )

    def test_deleting_what_is_not_there_is_not_a_failure(self):
        registry, outcome = self.apply(
            {"Software\\Demo": {"A": "1"}},
            r"reg delete HKLM\Software\Demo /v Absent /f",
            r"reg delete HKLM\Software\Ghost /f",
        )
        self.assertEqual((outcome.succeeded, outcome.failed), (2, 0))
        self.assertEqual(registry.snapshot()["Software\\Demo"], {"A": "1"})

    def test_an_add_creates_the_missing_key_and_writes_the_declared_type(self):
        registry, outcome = self.apply(
            {}, r"reg add HKLM\Software\New\Deep /v Count /t REG_DWORD /d 5 /f"
        )
        self.assertEqual((outcome.succeeded, outcome.failed), (1, 0))
        node = registry.key(HKLM, "Software\\New\\Deep")
        self.assertEqual(node.values["Count"], (5, sentinel.winreg.REG_DWORD))


class ResetSafetyTest(unittest.TestCase):
    LINES = (
        r"reg delete HKLM\Software\Demo /f",
        r"reg add HKLM\Software\Demo /v A /t REG_SZ /d 1 /f",
        r"reg add HKLM\Software\Demo\Sub /v B /t REG_SZ /d 2 /f",
    )

    def queue(self, blocked_line=None):
        entries = sentinel.RegistryCommandParser()._parse_stream(self.LINES).entries
        for entry in entries:
            if entry.source_line == blocked_line:
                entry.access_denied = True
        window = SimpleNamespace(_entries=entries)
        queued, unsafe = sentinel.RegistrySentinel._with_reset_members(window, [entries[0]])
        return [entry.source_line for entry in queued], unsafe

    def test_a_reset_rewrites_its_listed_values_after_the_delete(self):
        self.assertEqual(self.queue(), ([1, 2, 3], 0))

    def test_a_reset_is_skipped_when_a_listed_value_cannot_be_rewritten(self):
        self.assertEqual(self.queue(blocked_line=2), ([], 1))


if __name__ == "__main__":
    unittest.main()
