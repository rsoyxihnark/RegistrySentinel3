# Changelog

## 1.1.7

- A caret now escapes the character after it, so a `&`, `|` or `>` written as `^&`, `^|` or `^>` reaches the value as data instead of cutting the line short.

## 1.1.6

- A scan now checks which user profiles are loaded each time it runs, so an `HKU` entry is no longer judged against the profiles that were loaded when the list was opened.
- Loading a different list no longer leaves the result of the previous Apply sitting in the status line.
- When Go to UID clears the `reg add` / `reg delete` filter to reveal a row, that filter is now remembered next time.

## 1.1.5

- A `reg add` or `reg delete` that chains onto another command with no space around the `&`, `|` or `>` is now read normally, instead of being reported as a list error.
- A `reg delete` of a key followed by `reg add` lines for that key on the same line is now read as a reset, the same as when those lines come afterwards.
- A `reg add` that a later `reg delete` on the same line wipes is now reported as a list conflict.

## 1.1.4

- A scan now reads each registry key once, however many ways the list names it.

## 1.1.3

- A `reg add` or `reg delete` line that chains onto another command with `&`, `&&`, `|` or `||` is now read normally, instead of being reported as a list error.
- A line that runs more than one `reg` command now shows a row for each of them.

## 1.1.2

- The entry count at the bottom of the window is no longer cut off when the list contains errors.
- A `reg delete` line that carries a `/s` switch is now always reported as a list error, instead of sometimes being read as a delete of the whole key.
- The warning shown when only some fixes could be applied is now titled Apply: Partial Failure.

## 1.1.1

- The log and the settings Registry Sentinel remembers between runs sit in the same folder as the program itself, instead of under AppData.

## 1.1.0

- The log and the settings Registry Sentinel remembers between runs now sit in the same folder as the program itself, instead of under AppData.

## 1.0

- Registry Sentinel reads a list of `reg add` and `reg delete` commands and reports which ones the Windows registry already matches.
- Load the default List file sitting next to the program, or browse for any .txt, .bat or .cmd list.
- Press F5 to scan every loaded entry and see it marked compliant, non-compliant or pending.
- Apply the fix to the entries you tick, or to every visible non-compliant entry at once.
- A `reg delete` of a key followed by `reg add` lines for that key is treated as a reset, compliant only while the key holds nothing but the listed values.
- Entries whose declared value type disagrees with the registry are flagged so the list can be corrected before anything is applied.
- Lines that cannot be read, and lines that write the same target differently, are flagged as list errors and conflicts.
- Find rows with Ctrl+F, step through matches with F3, and go to a row by number with Ctrl+G.
- Right-click the type column header to show only `reg add` or only `reg delete` entries, and the choice is remembered next time.
- Right-click any row to copy its path, name, compliance detail or command, or to open the key in Regedit.
- Export the current results as a CSV report.
- The program offers to restart with administrator rights when it needs them, and keeps a log you can open from the toolbar.
