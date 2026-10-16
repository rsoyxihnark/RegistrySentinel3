import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_empty_quoted_data_followed_by_a_chain_stays_empty(self):
        for tail in ("&echo ok", "&&echo ok", "|more", ">nul"):
            entry = self.only(rf'reg add HKLM\Software\Foo /v A /t REG_SZ /d ""{tail}')
            self.assertFalse(entry.syntax_error, tail)
            self.assertEqual(entry.expected, "", tail)

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


class PercentTest(unittest.TestCase):
    def data(self, text, value_type="REG_SZ"):
        line = rf'reg add HKLM\Software\Foo /v A /t {value_type} /d "{text}" /f'
        entries = sentinel.RegistryCommandParser()._parse_stream([line]).entries
        self.assertEqual(len(entries), 1)
        self.assertFalse(entries[0].syntax_error, text)
        self.assertEqual(entries[0].raw_command, line)
        return entries[0].expected

    def setUp(self):
        patcher = mock.patch.dict(os.environ, {"RS_TEST_SET": "abcdef"})
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("RS_TEST_UNSET", None)

    def test_a_lone_percent_is_dropped_the_way_a_batch_file_drops_it(self):
        line = (
            r'reg add "HKEY_CURRENT_USER\SOFTWARE\Policies\Microsoft\Edge" /v "ManagedSearchEngines" '
            r'/t REG_SZ /d "[{"allow_search_engine_discovery": false}, {"is_default": true, '
            r'"keyword": "Google.com", "name": "Google", "search_url": "https://google.com/search?q=%s"}]" /f'
        )
        entries = sentinel.RegistryCommandParser()._parse_stream([line]).entries
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].raw_command, line)
        self.assertEqual(
            entries[0].expected,
            "[{allow_search_engine_discovery: false}, {is_default: true, keyword: Google.com, "
            "name: Google, search_url: https://google.com/search?q=s}]",
        )

    def test_a_doubled_percent_is_one_percent(self):
        self.assertEqual(self.data("q=%%s"), "q=%s")
        self.assertEqual(self.data("50%% off"), "50% off")
        self.assertEqual(self.data("100%"), "100")

    def test_a_variable_is_replaced_by_its_value(self):
        self.assertEqual(self.data(r"%RS_TEST_SET%\x"), r"abcdef\x")
        self.assertEqual(self.data(r"a%RS_TEST_UNSET%b"), "ab")

    def test_a_batch_argument_is_empty(self):
        self.assertEqual(self.data("a%1b"), "ab")
        self.assertEqual(self.data("a%*b"), "ab")
        self.assertEqual(self.data("a%20b"), "a0b")
        self.assertEqual(self.data("a%~1b"), "ab")

    def test_an_unset_variable_before_a_colon_is_dropped_up_to_the_colon(self):
        self.assertEqual(self.data("a%b:c"), "ac")

    def test_a_variable_substring_and_replacement_are_applied(self):
        self.assertEqual(self.data("%RS_TEST_SET:~1,3%"), "bcd")
        self.assertEqual(self.data("%RS_TEST_SET:~-2%"), "ef")
        self.assertEqual(self.data("%RS_TEST_SET:~0,-2%"), "abcd")
        self.assertEqual(self.data("%RS_TEST_SET:CD=x%"), "abxef")
        self.assertEqual(self.data("%RS_TEST_SET:*c=-%"), "-def")

    def test_an_expandable_string_keeps_the_variable_a_doubled_percent_protects(self):
        self.assertEqual(self.data(r"%%SystemRoot%%\x", "REG_EXPAND_SZ"), r"%SystemRoot%\x")

    def test_a_key_path_is_expanded_once(self):
        parse = sentinel.RegistryCommandParser()._parse_stream
        entry = parse([r"reg add HKLM\Software\%RS_TEST_SET% /v A /d 1 /f"]).entries[0]
        self.assertEqual(entry.path, r"Software\abcdef")
        entry = parse([r"reg add HKLM\Software\%%RS_TEST_SET%% /v A /d 1 /f"]).entries[0]
        self.assertEqual(entry.path, r"Software\%RS_TEST_SET%")


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

    def test_a_windows_ansi_list_keeps_its_own_characters(self):
        text = "\r\n".join(self.LINES).format("\u2019")
        try:
            data = text.encode("mbcs")
        except (UnicodeEncodeError, LookupError):
            self.skipTest("this system code page cannot hold the test text")
        result = self.parse_bytes(data)
        self.assertEqual(result.entries[0].expected, "Loading\u2019")

    def test_a_windows_ansi_list_keeps_each_command_whole(self):
        text = "\r\n".join(self.LINES).format("\u2026")
        self.check_one_command_per_line(text, "cp1252")


class RegNoneTest(unittest.TestCase):
    def only(self, line):
        entries = sentinel.RegistryCommandParser()._parse_stream([line]).entries
        self.assertEqual(len(entries), 1)
        return entries[0]

    def test_reg_none_is_read_as_raw_bytes(self):
        entry = self.only(r"reg add HKLM\Software\Foo /v A /t REG_NONE /d 0102ff /f")
        self.assertFalse(entry.syntax_error)
        self.assertIs(entry.value_type, sentinel.ValueType.NONE)
        self.assertEqual(entry.expected, "0102ff")
        self.assertEqual(sentinel._prepare_value(entry.expected, entry.value_type), b"\x01\x02\xff")

    def test_reg_none_without_data_is_empty(self):
        entry = self.only(r"reg add HKLM\Software\Foo /v A /t REG_NONE /f")
        self.assertEqual(entry.expected, "")
        self.assertEqual(sentinel._prepare_value(entry.expected, entry.value_type), b"")

    def test_reg_none_that_is_not_hex_is_a_list_error(self):
        self.assertTrue(self.only(r"reg add HKLM\Software\Foo /v A /t REG_NONE /d zz /f").syntax_error)

    def test_reg_none_compares_like_binary(self):
        self.assertTrue(sentinel._compare_expected("0102FF", "0102ff", sentinel.ValueType.NONE))
        self.assertFalse(sentinel._compare_expected("0102", "0102ff", sentinel.ValueType.NONE))


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
