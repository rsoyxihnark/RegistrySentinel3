# Changelog

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
