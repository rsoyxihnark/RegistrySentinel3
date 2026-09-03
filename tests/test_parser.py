import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import registry_sentinel as sentinel


class ParseTest(unittest.TestCase):
    def parse(self, *lines):
        return sentinel.RegistryCommandParser()._parse_stream(lines)

    def only(self, *lines):
        entries = self.parse(*lines).entries
        self.assertEqual(len(entries), 1)
        return entries[0]

    def test_chained_command_is_not_a_list_error(self):
        for tail in ("&& echo ok", "|| echo failed", "| more", "& echo ok"):
            entry = self.only(rf"reg add HKLM\Software\Foo /v A /d 1 /f {tail}")
            self.assertFalse(entry.syntax_error, tail)
            self.assertEqual(entry.value_name, "A")
            self.assertEqual(entry.expected, "1")

    def test_chained_command_without_spaces_is_not_a_list_error(self):
        for tail in ("&&echo ok", "||echo failed", "|more", "&echo ok", ">nul"):
            entry = self.only(rf"reg add HKLM\Software\Foo /v A /d 1 /f{tail}")
            self.assertFalse(entry.syntax_error, tail)
            self.assertEqual(entry.value_name, "A")

    def test_two_commands_on_one_line_both_parse(self):
        entries = self.parse(
            r"reg add HKLM\Software\Foo /v A /d 1 /f & reg delete HKLM\Software\Foo /v B /f"
        ).entries
        self.assertEqual([e.value_name for e in entries], ["A", "B"])
        self.assertEqual([e.operation for e in entries], [sentinel.Operation.ADD, sentinel.Operation.DELETE])
        self.assertEqual(len({e.unique_id for e in entries}), 2)

    def test_leading_non_registry_command_is_ignored(self):
        entry = self.only(r"echo applying & reg add HKLM\Software\Foo /v A /d 1 /f")
        self.assertEqual(entry.value_name, "A")

    def test_redirection_still_parses(self):
        entry = self.only(r"reg add HKLM\Software\Foo /v A /t REG_DWORD /d 4 /f >nul 2>&1")
        self.assertFalse(entry.syntax_error)
        self.assertIs(entry.value_type, sentinel.ValueType.DWORD)

    def test_separator_inside_quoted_data_is_kept(self):
        entry = self.only(r'reg add HKLM\Software\Foo /v A /t REG_SZ /d "one & two" /f')
        self.assertEqual(entry.expected, "one & two")

    def test_delete_with_recursive_switch_is_a_list_error(self):
        entry = self.only(r"reg delete HKLM\Software\Foo /s /f")
        self.assertTrue(entry.syntax_error)

    def test_line_without_a_registry_command_is_skipped(self):
        result = self.parse("echo nothing here")
        self.assertEqual(result.entries, [])
        self.assertEqual(len(result.skipped_lines), 1)

    def test_caret_escaped_separator_is_part_of_the_data(self):
        for tail, expected in (("a^&b", "a&b"), ("a^|b", "a|b"), ("a^>b", "a>b"), ("a^^b", "a^b")):
            entry = self.only(rf"reg add HKLM\Software\Foo /v A /t REG_SZ /d {tail} /f")
            self.assertFalse(entry.syntax_error, tail)
            self.assertEqual(entry.expected, expected, tail)

    def test_caret_inside_quotes_is_literal(self):
        entry = self.only(r'reg add HKLM\Software\Foo /v A /t REG_SZ /d "a^&b" /f')
        self.assertEqual(entry.expected, "a^&b")

    def test_caret_before_an_ordinary_character_is_dropped(self):
        entry = self.only(r"reg add HKLM\Software\Foo /v A /t REG_SZ /d a^b /f")
        self.assertEqual(entry.expected, "ab")

    def test_caret_escaped_separator_standing_alone_is_a_list_error(self):
        for tail in ("^& echo hi", "^> out.txt", "2^>&1"):
            entry = self.only(rf"reg add HKLM\Software\Foo /v A /d 1 /f {tail}")
            self.assertTrue(entry.syntax_error, tail)


class ListFileTest(unittest.TestCase):
    LINES = (
        r'reg add HKLM\Software\Foo /v A /t REG_SZ /d "Loading{0}" /f',
        r"reg add HKLM\Software\Foo /v B /t REG_SZ /d 2 /f",
    )

    def parse_bytes(self, data):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "List.txt"
            path.write_bytes(data)
            return sentinel.RegistryCommandParser().parse_file(path)

    def check_one_command_per_line(self, text, encoding):
        result = self.parse_bytes(text.encode(encoding))
        self.assertEqual(result.skipped_lines, [])
        self.assertEqual([e.value_name for e in result.entries], ["A", "B"])
        self.assertEqual([e.source_line for e in result.entries], [1, 2])
        self.assertFalse(any(e.syntax_error for e in result.entries))
        self.assertEqual(len(result.entries[0].expected), len("Loading") + 1)

    def test_a_line_holding_an_unusual_character_is_not_split(self):
        for filler in ("\u0085", "\u000c", "\u000b", "\u2028"):
            with self.subTest(filler=filler):
                text = "\r\n".join(self.LINES).format(filler)
                self.check_one_command_per_line(text, "latin-1" if filler < "\u0100" else "utf-8")

    def test_a_windows_ansi_list_keeps_each_command_whole(self):
        text = "\r\n".join(self.LINES).format("\u2026")
        self.check_one_command_per_line(text, "cp1252")


class ListErrorTest(unittest.TestCase):
    def parse(self, *lines):
        return sentinel.RegistryCommandParser()._parse_stream(lines).entries

    def test_same_value_written_differently_conflicts(self):
        entries = self.parse(
            r"reg add HKLM\Software\Foo /v A /t REG_SZ /d 1 /f",
            r"reg add HKLM\Software\Foo /v A /t REG_SZ /d 2 /f",
        )
        self.assertTrue(all(e.conflict for e in entries))

    def test_same_value_written_identically_does_not_conflict(self):
        entries = self.parse(
            r"reg add HKLM\Software\Foo /v A /t REG_DWORD /d 1 /f",
            r"reg add HKLM\Software\Foo /v A /t REG_DWORD /d 0x1 /f",
        )
        self.assertFalse(any(e.conflict for e in entries))

    def test_key_delete_followed_by_adds_is_a_reset(self):
        delete, first, second = self.parse(
            r"reg delete HKLM\Software\Foo /f",
            r"reg add HKLM\Software\Foo /v A /t REG_SZ /d 1 /f",
            r"reg add HKLM\Software\Foo\Sub /v B /t REG_SZ /d 2 /f",
        )
        self.assertTrue(delete.is_reset)
        self.assertIs(delete.value_type, sentinel.ValueType.RESET)
        self.assertEqual(delete.reset_plan.member_ids, [first.unique_id, second.unique_id])
        self.assertEqual(delete.reset_plan.keys, {"sub"})

    def test_key_delete_before_an_add_on_the_same_line_is_a_reset(self):
        delete, add = self.parse(
            r"reg delete HKLM\Software\Foo /f & reg add HKLM\Software\Foo /v A /d 1 /f"
        )
        self.assertTrue(delete.is_reset)
        self.assertEqual(delete.reset_plan.member_ids, [add.unique_id])
        self.assertFalse(add.conflict)

    def test_key_delete_after_an_add_on_the_same_line_wipes_it(self):
        add, delete = self.parse(
            r"reg add HKLM\Software\Foo /v A /d 1 /f & reg delete HKLM\Software\Foo /f"
        )
        self.assertTrue(add.conflict)
        self.assertTrue(delete.conflict)
        self.assertFalse(delete.is_reset)

    def test_key_delete_after_an_add_wipes_it(self):
        add, delete = self.parse(
            r"reg add HKLM\Software\Foo /v A /t REG_SZ /d 1 /f",
            r"reg delete HKLM\Software\Foo /f",
        )
        self.assertTrue(add.conflict)
        self.assertTrue(delete.conflict)
        self.assertFalse(delete.is_reset)


if __name__ == "__main__":
    unittest.main()
