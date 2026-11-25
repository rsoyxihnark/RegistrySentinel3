# Changelog

## 1.6.0

- The toolbar buttons and the search box now have sharp matching icons in place of the old Windows ones.
- Registry Sentinel now has its own shield icon on the file in File Explorer, on the title bar and on the taskbar.
- The title bar is now dark to match the rest of the window.
- The scroll bars and the strip beside the last column header are now dark to match the table.
- The column the table is sorted by is now underlined in its header.
- The dividers between the toolbar button groups now match the dark toolbar.
- An empty table now says why: no list loaded, no reg add or reg delete lines in the list, or every entry hidden.
- When every entry is compliant and Compliant is switched off, the table now says so instead of showing nothing.
- The Compliant, Non-Compliant and Pending switches now dim their label when switched off and fade smoothly between on and off.
- Reset View, View Log and Export CSV Report now say what they do when you rest the pointer on them.

## 1.5.0

- A `set NAME=value` line in a .bat or .cmd list now gives `%NAME%` that value in the lines after it, the same as in a batch file.
- A `reg delete` of a whole user or system hive, such as `HKU\*` or `HKLM\SOFTWARE`, is now reported as a list error instead of being applied.
- Fixed the window reopening oversized and no longer maximized, or on another monitor, when Registry Sentinel was closed while maximized.

## 1.4.2

- Fixed a list error being reported for a `reg` line that sends its output to a quoted file path holding a space.
- Fixed a quoted number with `>` straight after it, such as `/d "5">nul`, being written as `5>nul` to a REG_SZ value and reported as a list error for a REG_DWORD value.

## 1.4.1

- Fixed a list error's message showing twice when you rest the pointer on a Details cell too narrow for it.
- Fixed the right-click menus of the table and its column headers being kept for as long as the program is running.
- The list conflict tooltip on the Select box is now shorter, and still names each kind of conflict and how to fix it.

## 1.4.0

- The Help button is gone from the toolbar, and F1 no longer opens the help window.

## 1.3.0

- A long path or detail in the table no longer breaks off with three dots partway across its column, and now runs to the full width.
- Resting the pointer on a cell too narrow for its text now shows the whole of it, so a long path can be read without widening the column.

## 1.2.5

- A lone `%` in a list line is now dropped the way a batch file drops it, so `search?q=%s` is no longer reported as wrong when the registry holds `search?q=s`.
- A `%%` in a list line is now read as one `%`, so a scan expects one `%` and Apply writes one, the same as a batch file.
- A variable such as `%USERPROFILE%` in a list line is now replaced by its value, and one that is not set is left out, the same as in a batch file.
- A `%1` to `%9` or `%*` in a list line is now left out, the same as in a batch file run without arguments.

## 1.2.4

- A scan of a reset key that names a great many subkeys no longer ties up Windows resources while it runs.
- The messages and warnings Registry Sentinel shows are now let go of once you close them, instead of being kept for as long as the program is running.

## 1.2.3

- A `reg add` whose data is an empty pair of quotes with `&`, `|` or `>` straight after it now writes an empty value, instead of writing the rest of the line into the registry or reporting the line as a list error.
- Apply Selected no longer warns that ticked entries are hidden when those entries have nothing left to fix.

## 1.2.2

- Registry Sentinel now carries its version number, so File Explorer shows it in the File version column and it appears on the file's Properties page.

## 1.2.1

- A compliant entry whose value was found in the VirtualStore now says so, instead of looking as though the value sits in the key the list names.

## 1.2.0

- A `reg add` line that writes a `REG_NONE` value is now read and checked like any other, instead of being reported as a list error.
- A list saved in the Windows ANSI encoding now keeps its own characters, so a value holding something like a curly quote is written the way it was typed.
- Showing only `reg add` or only `reg delete` entries now brings the Type column into view, so the filter that hides the rest is plain to see on the next run.
- Apply Selected now leaves alone any ticked entry the current filter hides, and says how many, so nothing is written that you cannot see.

## 1.1.8

- A line holding a character such as `…` is now read as one command, instead of being split in two so that the value was written short and the rest of the line was ignored.

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
