from __future__ import annotations

import configparser
import csv
import ctypes
import fnmatch
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from collections import defaultdict
from contextlib import suppress
from concurrent.futures import FIRST_COMPLETED, CancelledError, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from enum import Enum
from functools import partialmethod
from itertools import count
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence, Union

if os.name != "nt":
    print("Registry Sentinel runs on Windows only.")
    sys.exit(1)

import winreg

from PyQt6.QtCore import (
    QEasingCurve,
    QEvent,
    QItemSelectionModel,
    QPoint,
    QPropertyAnimation,
    QRect,
    QSize,
    Qt,
    QtMsgType,
    QThread,
    QTimer,
    pyqtProperty,
    pyqtSignal,
    qInstallMessageHandler,
    QMessageLogContext,
)
from PyQt6.QtGui import (
    QAction,
    QBrush,
    QColor,
    QFont,
    QFontMetrics,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPen,
    QPolygon,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFileDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


LOG_FILENAME = "sentinel.log"
APP_VERSION = "1.1.4"
logger = logging.getLogger(__name__)
_qt_logger = logging.getLogger("PyQt6")


def _program_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def _default_log_path() -> Path:
    return _program_dir() / LOG_FILENAME


def _log_uncaught_exception(exc_type, exc_value, exc_traceback) -> None:
    text = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))
    logger.critical("Uncaught exception:\n%s", text.rstrip())
    sys.__excepthook__(exc_type, exc_value, exc_traceback)


def _qt_message_handler(mode: QtMsgType, context: QMessageLogContext, message: str) -> None:
    level_map = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }
    level = level_map.get(mode, logging.INFO)
    details: list[str] = []
    if context.file and context.line:
        details.append(f"{context.file}:{context.line}")
    elif context.file:
        details.append(context.file)
    if context.function:
        details.append(context.function)
    if context.category:
        details.append(context.category)
    suffix = f" ({'; '.join(details)})" if details else ""
    _qt_logger.log(level, "%s%s", message, suffix)


def init_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

    def _ensure_console_handler() -> None:
        if sys.stdout is None:
            return
        has_console = any(
            isinstance(handler, logging.StreamHandler)
            and getattr(handler, "stream", None) is sys.stdout
            for handler in root.handlers
        )
        if has_console:
            return
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(fmt)
        root.addHandler(console)

    needs_file_handler = not any(
        isinstance(handler, RotatingFileHandler) for handler in root.handlers
    )

    if needs_file_handler:
        log_path = _default_log_path()
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handler = RotatingFileHandler(
                log_path,
                maxBytes=1_000_000,
                backupCount=3,
                encoding="utf-8",
            )
            handler.setFormatter(fmt)
            root.addHandler(handler)
        except OSError as exc:
            print(f"Warning: unable to initialise log file {log_path}: {exc}")

    _ensure_console_handler()

    sys.excepthook = _log_uncaught_exception
    qInstallMessageHandler(_qt_message_handler)


class Operation(Enum):
    ADD = "ADD"
    DELETE = "DELETE"


class ValueType(Enum):
    SZ = "REG_SZ"
    EXPAND_SZ = "REG_EXPAND_SZ"
    DWORD = "REG_DWORD"
    QWORD = "REG_QWORD"
    BINARY = "REG_BINARY"
    MULTI_SZ = "REG_MULTI_SZ"
    DELETE = "DELETE"
    RESET = "RESET"
    KEY = "KEY"
    ERROR = "ERROR"


HIVE_NAMES: dict[str, int] = {
    "HKEY_CLASSES_ROOT": winreg.HKEY_CLASSES_ROOT,
    "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
    "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
    "HKEY_USERS": winreg.HKEY_USERS,
    "HKEY_CURRENT_CONFIG": winreg.HKEY_CURRENT_CONFIG,
    "HKCR": winreg.HKEY_CLASSES_ROOT,
    "HKCU": winreg.HKEY_CURRENT_USER,
    "HKLM": winreg.HKEY_LOCAL_MACHINE,
    "HKU": winreg.HKEY_USERS,
    "HKCC": winreg.HKEY_CURRENT_CONFIG,
}

REVERSE_HIVES: dict[int, str] = {handle: name for name, handle in HIVE_NAMES.items() if name.startswith("HKEY")}

REG_VIEW_64 = getattr(winreg, "KEY_WOW64_64KEY", 0)
REG_READ_64 = winreg.KEY_READ | REG_VIEW_64
REG_WRITE_64 = winreg.KEY_SET_VALUE | REG_VIEW_64
REG_VIEW_32 = getattr(winreg, "KEY_WOW64_32KEY", 0)
REG_READ_32 = winreg.KEY_READ | REG_VIEW_32
REG_WRITE_32 = winreg.KEY_SET_VALUE | REG_VIEW_32

DEFAULT_VIEW_LABEL = "64-bit view"

READ_VIEW_OPTIONS: tuple[tuple[int, str], ...] = (
    (REG_READ_64, DEFAULT_VIEW_LABEL),
    (REG_READ_32, "32-bit view"),
)

WRITE_VIEW_FLAGS: tuple[int, ...] = (REG_WRITE_64, REG_WRITE_32)

DELETE_VIEW_FLAGS: tuple[tuple[int, int], ...] = (
    (REG_VIEW_64, REG_WRITE_64),
    (REG_VIEW_32, REG_WRITE_32),
)

_VIEW_INDEX = {None: 0, 64: 0, 32: 1}


def _view_option(options: Sequence[Any], view: Optional[int]) -> Any:
    return options[_VIEW_INDEX[view]]


@dataclass(slots=True)
class OpenKeyResult:
    handle: Optional[Any]
    label: Optional[str]
    denied: Optional[str] = None
    access: int = 0
    error: Optional[OSError] = None


VIRTUALSTORE_PREFIX = r"Software\Classes\VirtualStore"
VIRTUALSTORE_MACHINE_SUFFIX = "MACHINE"
VIRTUALSTORE_CLASSES_SUFFIX = "CLASSES"
VIRTUALSTORE_MACHINE_PREFIX = f"{VIRTUALSTORE_PREFIX}\\{VIRTUALSTORE_MACHINE_SUFFIX}"
VIRTUALSTORE_CLASSES_PREFIX = f"{VIRTUALSTORE_PREFIX}\\{VIRTUALSTORE_CLASSES_SUFFIX}"
VIRTUAL_PREFIX_MAP = {
    winreg.HKEY_LOCAL_MACHINE: VIRTUALSTORE_MACHINE_PREFIX,
    winreg.HKEY_CLASSES_ROOT: VIRTUALSTORE_CLASSES_PREFIX,
}

REG_BRANCH_ACCESS = winreg.KEY_ENUMERATE_SUB_KEYS | winreg.KEY_QUERY_VALUE

MAX_SCAN_WORKERS = 32
PROGRESS_EMIT_INTERVAL_S = 0.05

CREATE_NO_WINDOW = 0x08000000
TASKKILL_TIMEOUT_S = 5
SW_SHOWNORMAL = 1
SHELL_EXECUTE_ERROR_THRESHOLD = 32

_shell_execute = ctypes.windll.shell32.ShellExecuteW
_shell_execute.restype = ctypes.c_ssize_t

_WINDIR = Path(os.environ.get("SystemRoot", r"C:\Windows"))
TASKKILL_PATH = _WINDIR / "System32" / "taskkill.exe"
NOTEPAD_PATH = _WINDIR / "System32" / "notepad.exe"
REGEDIT_PATH = _WINDIR / "regedit.exe"

_WINREG_TYPE_MAP = {
    ValueType.SZ: winreg.REG_SZ,
    ValueType.EXPAND_SZ: winreg.REG_EXPAND_SZ,
    ValueType.DWORD: winreg.REG_DWORD,
    ValueType.QWORD: winreg.REG_QWORD,
    ValueType.BINARY: winreg.REG_BINARY,
    ValueType.MULTI_SZ: winreg.REG_MULTI_SZ,
}

_REG_TYPE_NAMES = {code: vt.value for vt, code in _WINREG_TYPE_MAP.items()}

TYPE_MISMATCH_MARK = "▲"
TYPE_MISMATCH_TAG = f"{TYPE_MISMATCH_MARK} TYPE MISMATCH"
FIX_LIST_HINT = "FIX THE LIST BEFORE APPLYING"
TYPE_MISMATCH_TOOLTIP = (
    "Type mismatch: this entry cannot be applied.\n"
    "The list declares a value type the registry does not use; correct the list first."
)


SYNTAX_ERROR_TAG = f"{TYPE_MISMATCH_MARK} LIST SYNTAX ERROR"
SYNTAX_ERROR_TOOLTIP = (
    "List syntax error: this line cannot be verified or applied.\n"
    "Correct the command in the list file."
)

CONFLICT_TAG = f"{TYPE_MISMATCH_MARK} LIST CONFLICT"
CONFLICT_LINE_DISPLAY = 4
CONFLICT_TOOLTIP = (
    "List conflict: other lines write this same target differently.\n"
    "Two 'reg add' lines with different data, a 'reg add' and a 'reg delete' for the same "
    "value, or a key delete that wipes what an earlier line writes: whichever line runs "
    "last wins, so the list has no defined result and every scan flips the other line back "
    "to non-compliant. Remove the wrong line."
)

RESET_EXPECTED_TEXT = "Only listed values"
RESET_EXTRA_DISPLAY = 4
RESET_EXTRA_LIMIT = 16
RESET_MAX_NODES = 20_000
RESET_TOOLTIP = (
    "Reset key: this 'reg delete' is followed by 'reg add' lines for the same key.\n"
    "It is compliant while the key holds nothing but the values the list adds.\n"
    "Applying it wipes the key and rewrites every listed value."
)

DEFAULT_VALUE_LABEL = "<default>"
DEFAULT_VALUE_SEARCH_ALIAS = "(default)"
DEFAULT_VALUE_TOOLTIP = (
    "The key's own default value, written with '/ve'.\n"
    "regedit shows it as (Default) and .reg files write it as '@'. A value literally named "
    "(Default) is a different value and appears under that name instead."
)
LITERAL_DEFAULT_TOOLTIP = (
    "A value literally named (Default), created by '/v \"(Default)\"'.\n"
    "reg.exe takes /v names literally, so this is an ordinary value that merely looks like "
    "the key's default. Use '/ve' if you meant the key's own default value."
)


def _value_label(name: str) -> str:
    return DEFAULT_VALUE_LABEL if not name else name


def _type_mismatch_detail(expected: str, found: str) -> str:
    return (
        f"{TYPE_MISMATCH_TAG}: LIST SAYS {expected.upper()}, "
        f"REGISTRY HAS {found.upper()}. {FIX_LIST_HINT}"
    )


def _syntax_error_detail(reason: str) -> str:
    return f"{SYNTAX_ERROR_TAG}: {reason.upper()}. {FIX_LIST_HINT}"


def _format_conflict_lines(lines: Sequence[int]) -> tuple[str, str]:
    shown = ", ".join(str(line) for line in lines[:CONFLICT_LINE_DISPLAY])
    if len(lines) > CONFLICT_LINE_DISPLAY:
        shown += ", …"
    return ("LINE" if len(lines) == 1 else "LINES"), shown


def _conflict_detail(other_lines: Sequence[int]) -> str:
    label, shown = _format_conflict_lines(other_lines)
    verb = "TARGETS" if len(other_lines) == 1 else "TARGET"
    return f"{CONFLICT_TAG}: {label} {shown} {verb} THIS VALUE DIFFERENTLY. {FIX_LIST_HINT}"


def _wiped_detail(delete_line: int, target: str) -> str:
    return f"{CONFLICT_TAG}: LINE {delete_line} DELETES THIS {target} AFTERWARDS. {FIX_LIST_HINT}"


def _wipes_detail(add_lines: Sequence[int]) -> str:
    label, shown = _format_conflict_lines(add_lines)
    verb = "WRITES" if len(add_lines) == 1 else "WRITE"
    return f"{CONFLICT_TAG}: THIS DELETE WIPES WHAT {label} {shown} {verb}. {FIX_LIST_HINT}"


_INT_MASKS = {
    ValueType.DWORD: 0xFFFFFFFF,
    ValueType.QWORD: 0xFFFFFFFF_FFFFFFFF,
}

_ENV_VAR_PATTERN = re.compile(r"%([^%\r\n]+)%")


def _expand_env_vars(text: str) -> str:
    return _ENV_VAR_PATTERN.sub(lambda match: os.environ.get(match.group(1), match.group(0)), text)


def _format_reg_int_display(text: str, value_type: ValueType) -> str:
    if value_type not in _INT_MASKS or not text:
        return text
    try:
        number = _parse_reg_int(text) & _INT_MASKS[value_type]
    except ValueError:
        return text
    width = 16 if value_type == ValueType.QWORD else 8
    return f"0x{number:0{width}x} ({number})"


@dataclass(slots=True)
class ResetPlan:
    values: dict[str, set[str]] = field(default_factory=dict)
    keys: set[str] = field(default_factory=set)
    member_ids: list[str] = field(default_factory=list)

    @property
    def declared_count(self) -> int:
        return len(self.member_ids)

    def allow(self, relative_path: str, value_name: Optional[str]) -> None:
        parts = [part for part in relative_path.split("\\") if part]
        for depth in range(1, len(parts) + 1):
            self.keys.add("\\".join(parts[:depth]))
        if value_name is not None:
            self.values.setdefault(relative_path, set()).add(value_name.casefold())


def _normalize_key_path(path: str) -> str:
    return (path or "").strip("\\").casefold()


def _views_compatible(first: Optional[int], second: Optional[int]) -> bool:
    return _VIEW_INDEX[first] == _VIEW_INDEX[second]


_ENTRY_SEQUENCE = count()


@dataclass(slots=True)
class RegistryEntry:
    hive: int
    path: str
    value_name: Optional[str]
    value_type: ValueType
    expected: Optional[str]
    operation: Operation
    source_line: int
    actual: Optional[str] = None
    compliant: Optional[bool] = None
    detail: str = ""
    selected: bool = field(default=False)
    raw_command: str = ""
    view: Optional[int] = None
    type_mismatch: bool = False
    syntax_error: bool = False
    access_denied: bool = False
    all_values: bool = False
    conflict: bool = False
    reset_plan: Optional[ResetPlan] = None
    unique_id: str = field(init=False)
    search_blob: str = field(init=False, repr=False, default="")

    def __post_init__(self) -> None:
        self.unique_id = str(next(_ENTRY_SEQUENCE))
        self.refresh_search_blob()

    @property
    def hive_name(self) -> str:
        return REVERSE_HIVES.get(self.hive, "UNKNOWN")

    @property
    def list_error(self) -> bool:
        return self.type_mismatch or self.syntax_error or self.conflict

    @property
    def is_reset(self) -> bool:
        return self.reset_plan is not None

    @property
    def registry_path(self) -> str:
        return f"{self.hive_name}\\{self.path}" if self.path else self.hive_name

    @property
    def full_path(self) -> str:
        return self.raw_command if self.syntax_error else self.registry_path

    @property
    def display_name(self) -> str:
        if self.syntax_error:
            return ""
        if self.all_values:
            return "<all values>"
        if self.value_name is None:
            return "<key>"
        return _value_label(self.value_name)

    @property
    def is_default_value(self) -> bool:
        return self.value_name == ""

    @property
    def has_literal_default_name(self) -> bool:
        return bool(self.value_name) and self.value_name.casefold() == "(default)"

    @property
    def detail_text(self) -> str:
        return self.detail or "Not scanned"

    @property
    def expected_text(self) -> str:
        return self.expected or ""

    @property
    def actual_text(self) -> str:
        return self.actual or ""

    @property
    def expected_display(self) -> str:
        return _format_reg_int_display(self.expected_text, self.value_type)

    @property
    def actual_display(self) -> str:
        if self.type_mismatch:
            return self.actual_text
        return _format_reg_int_display(self.actual_text, self.value_type)

    def refresh_search_blob(self) -> None:
        parts = [
            str(self.source_line),
            self.full_path,
            self.display_name,
            DEFAULT_VALUE_SEARCH_ALIAS if self.is_default_value else "",
            self.value_type.value,
            self.expected_text,
            self.actual_text,
            self.expected_display,
            self.actual_display,
            self.detail_text,
        ]
        self.search_blob = "|".join(str(part) for part in parts).casefold()


@dataclass(frozen=True, slots=True)
class SortKeys:
    path: str
    value: str
    then_by_value: tuple[Any, ...]
    then_by_path: tuple[Any, ...]


def _sort_keys(entry: RegistryEntry) -> SortKeys:
    path = entry.full_path.casefold()
    value = entry.display_name.casefold()
    return SortKeys(
        path=path,
        value=value,
        then_by_value=(value, entry.source_line),
        then_by_path=(path, value, entry.source_line),
    )


@dataclass(slots=True)
class ScanResult:
    entry_id: str
    actual: Optional[str]
    compliant: Optional[bool]
    detail: str
    type_mismatch: bool = False
    access_denied: bool = False


@dataclass(slots=True)
class ParseResult:
    entries: list[RegistryEntry]
    skipped_lines: list[tuple[int, str]]


@dataclass(slots=True)
class ExecutionOutcome:
    succeeded: int
    failed: int
    errors: list[str]
    skipped: int = 0
    denied_ids: list[str] = field(default_factory=list)


def _raise_if_cancelled(flag: threading.Event) -> None:
    if flag.is_set():
        raise CancelledError()


def _virtualized_location(hive: int, path: str) -> Optional[tuple[int, str]]:
    normalized = (path or "").strip("\\")
    if not normalized:
        return None
    prefix = VIRTUAL_PREFIX_MAP.get(hive)
    if not prefix:
        return None
    return (winreg.HKEY_CURRENT_USER, f"{prefix}\\{normalized}")


def _open_key_in_view(hive: int, path: str, access: int, label: str) -> OpenKeyResult:
    try:
        return OpenKeyResult(winreg.OpenKey(hive, path, 0, access), label, None, access)
    except PermissionError:
        return OpenKeyResult(None, None, label, access)
    except FileNotFoundError:
        return OpenKeyResult(None, None, None, access)
    except OSError as exc:
        return OpenKeyResult(None, None, None, access, exc)


def _open_virtual_store_key(hive: int, path: str, access: int) -> Optional[Any]:
    virt = _virtualized_location(hive, path)
    if virt:
        try:
            return winreg.OpenKey(virt[0], virt[1], 0, access)
        except OSError:
            pass
    return None


def _join_sub_path(parent: str, child: str) -> str:
    return f"{parent}\\{child}" if parent else child


def _enumerate_names(handle, reader: Callable[[Any, int], Any]) -> list[str]:
    names: list[str] = []
    with suppress(OSError):
        while True:
            item = reader(handle, len(names))
            names.append(item[0] if isinstance(item, tuple) else item)
    return names


_hku_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
_HKU_CACHE_TTL = 300.0


def _cached_hku_subkeys(pattern: str) -> tuple[str, ...]:
    now = time.monotonic()
    cached = _hku_cache.get(pattern)
    if cached and (now - cached[0]) < _HKU_CACHE_TTL:
        return cached[1]
    matches: list[str] = []
    try:
        with winreg.OpenKey(winreg.HKEY_USERS, "", 0, winreg.KEY_READ) as handle:
            index = 0
            while True:
                try:
                    subkey = winreg.EnumKey(handle, index)
                except OSError:
                    break
                index += 1
                if fnmatch.fnmatch(subkey, pattern):
                    matches.append(subkey)
    except OSError as exc:
        logger.warning("Unable to enumerate HKEY_USERS subkeys: %s", exc)
    result = tuple(matches)
    _hku_cache[pattern] = (now, result)
    return result


class HiveNotLoadedError(OSError):
    def __init__(self, hive: int, root: str) -> None:
        hive_name = REVERSE_HIVES.get(hive, hex(hive))
        super().__init__(
            f"'{root}' is not present under {hive_name} "
            f"(Windows cannot create a key directly beneath {hive_name})"
        )


def _hive_root_missing(hive: int, path: str) -> Optional[str]:
    if hive != winreg.HKEY_USERS:
        return None
    root = (path or "").strip("\\").split("\\", 1)[0]
    if not root:
        return None
    loaded = _cached_hku_subkeys("*")
    if not loaded:
        return None
    return None if root.casefold() in {n.casefold() for n in loaded} else root


def _csv_safe(value: str) -> str:
    return f"'{value}" if value.startswith(("=", "+", "-", "@", "\t", "\r")) else value


def _annotate_results(results: list[ScanResult], source: Optional[str]) -> None:
    if not source or source == DEFAULT_VIEW_LABEL:
        return
    suffix = f" [{source}]"
    for result in results:
        if result.compliant is True:
            continue
        tag = suffix.upper() if result.type_mismatch else suffix
        if tag not in result.detail:
            result.detail = (result.detail + tag).strip()


class _LineSyntaxError(Exception):
    pass


def _syntax_error_entry(
    line: str, line_number: int, reason: str, *, hive: int = 0, path: str = ""
) -> RegistryEntry:
    return RegistryEntry(
        hive=hive,
        path=path,
        value_name=None,
        value_type=ValueType.ERROR,
        expected="",
        operation=Operation.ADD,
        source_line=line_number,
        detail=_syntax_error_detail(reason),
        raw_command=line,
        syntax_error=True,
    )


def _key_deletes_by_root(
    entries: Sequence[RegistryEntry],
) -> dict[tuple[int, str], list[RegistryEntry]]:
    by_root: dict[tuple[int, str], list[RegistryEntry]] = defaultdict(list)
    for entry in entries:
        if (
            entry.operation is Operation.DELETE
            and entry.value_name is None
            and not entry.syntax_error
        ):
            root = _normalize_key_path(entry.path)
            if root:
                by_root[(entry.hive, root)].append(entry)
    return by_root


def _adds_under_key_deletes(
    entries: Sequence[RegistryEntry],
    by_root: dict[tuple[int, str], list[RegistryEntry]],
    *,
    add_runs_last: bool,
) -> Iterable[tuple[RegistryEntry, RegistryEntry, str]]:
    for add in entries:
        if add.operation is not Operation.ADD or add.syntax_error:
            continue
        normalized = _normalize_key_path(add.path)
        if not normalized:
            continue
        parts = normalized.split("\\")
        for depth in range(len(parts), 0, -1):
            root = "\\".join(parts[:depth])
            for delete in by_root.get((add.hive, root), ()):
                ordered = (
                    add.source_line > delete.source_line
                    if add_runs_last
                    else add.source_line < delete.source_line
                )
                if not ordered or not _views_compatible(delete.view, add.view):
                    continue
                relative = normalized[len(root) + 1 :]
                if delete.all_values and (relative or add.value_name is None):
                    continue
                yield delete, add, relative


def _link_reset_plans(entries: Sequence[RegistryEntry]) -> None:
    by_root = _key_deletes_by_root(entries)
    if not by_root:
        return

    plans: dict[str, ResetPlan] = {}
    for delete, add, relative in _adds_under_key_deletes(entries, by_root, add_runs_last=True):
        plan = plans.setdefault(delete.unique_id, ResetPlan())
        plan.allow(relative, add.value_name)
        plan.member_ids.append(add.unique_id)

    for delete in entries:
        plan = plans.get(delete.unique_id)
        if plan is None:
            continue
        delete.reset_plan = plan
        delete.value_type = ValueType.RESET
        delete.expected = RESET_EXPECTED_TEXT
        delete.refresh_search_blob()
        logger.info(
            "Reset pair: line %d empties %s and %d later line(s) repopulate it",
            delete.source_line,
            delete.registry_path,
            plan.declared_count,
        )


def _mark_conflict(entry: RegistryEntry, detail: str) -> None:
    if entry.conflict:
        return
    entry.conflict = True
    entry.detail = detail
    entry.refresh_search_blob()


def _flag_conflicting_entries(entries: Sequence[RegistryEntry]) -> None:
    _flag_value_conflicts(entries)
    _flag_wipe_conflicts(entries)


def _flag_value_conflicts(entries: Sequence[RegistryEntry]) -> None:
    groups: dict[tuple[int, str, int, str], list[RegistryEntry]] = defaultdict(list)
    for entry in entries:
        if entry.value_name is None or entry.syntax_error:
            continue
        key = (
            entry.hive,
            _normalize_key_path(entry.path),
            _VIEW_INDEX[entry.view],
            entry.value_name.casefold(),
        )
        groups[key].append(entry)

    for group in groups.values():
        first = group[0]
        if len(group) < 2 or all(_writes_same_data(first, other) for other in group[1:]):
            continue
        for entry in group:
            _mark_conflict(entry, _conflict_detail([o.source_line for o in group if o is not entry]))
        logger.warning(
            "List conflict: lines %s target %s\\%s differently",
            ", ".join(str(entry.source_line) for entry in group),
            first.registry_path,
            first.display_name,
        )


def _flag_wipe_conflicts(entries: Sequence[RegistryEntry]) -> None:
    by_root = _key_deletes_by_root(entries)
    if not by_root:
        return

    wiped_by_delete: dict[str, list[RegistryEntry]] = defaultdict(list)
    for delete, add, _relative in _adds_under_key_deletes(entries, by_root, add_runs_last=False):
        wiped_by_delete[delete.unique_id].append(add)

    for delete in entries:
        wiped = wiped_by_delete.get(delete.unique_id)
        if not wiped:
            continue
        for add in wiped:
            _mark_conflict(
                add,
                _wiped_detail(
                    delete.source_line, "KEY" if add.value_name is None else "VALUE"
                ),
            )
        wiped_lines = sorted({add.source_line for add in wiped})
        _mark_conflict(delete, _wipes_detail(wiped_lines))
        logger.warning(
            "List conflict: line %d deletes %s and wipes what line(s) %s write",
            delete.source_line,
            delete.registry_path,
            ", ".join(str(line) for line in wiped_lines),
        )


def _writes_same_data(first: RegistryEntry, second: RegistryEntry) -> bool:
    return first.value_type is second.value_type and _compare_expected(
        first.expected_text, second.expected_text, first.value_type
    )


class RegistryCommandParser:
    COMMENT_PREFIXES = ("#", "//", ";", "::")
    REM_COMMENT = re.compile(r"rem(?:\s|$)", re.IGNORECASE)

    _REDIRECT = re.compile(r"\d*>{1,2}(?:&\d+|\S+)?")
    _SEPARATORS = frozenset(("&", "&&", "|", "||"))
    _OPERATIONS = {"add": Operation.ADD, "delete": Operation.DELETE}
    _UNVERIFIED_OPERATIONS = frozenset(
        ("query", "copy", "save", "restore", "load", "unload", "compare", "export", "import", "flags")
    )
    _COMMAND_NAMES = frozenset(("reg", "reg.exe"))
    _ADD_ARGUMENT_SWITCHES = frozenset(("/v", "/t", "/d", "/s"))
    _DELETE_ARGUMENT_SWITCHES = frozenset(("/v",))

    def parse_file(self, file_path: Path) -> ParseResult:
        logger.info("Loading registry command list: %s", file_path)
        _hku_cache.clear()
        raw = file_path.read_bytes()
        if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
            text = raw.decode("utf-16")
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("latin-1")
        result = self._parse_stream(text.splitlines())
        logger.info("Loaded %d registry entries, %d lines skipped", len(result.entries), len(result.skipped_lines))
        return result

    def _parse_stream(self, stream: Iterable[str]) -> ParseResult:
        entries: list[RegistryEntry] = []
        skipped: list[tuple[int, str]] = []
        for index, raw_line in enumerate(stream, start=1):
            line = raw_line.strip()
            if line.startswith("@"):
                line = line[1:].strip()
            if not line or line.startswith(self.COMMENT_PREFIXES) or self.REM_COMMENT.match(line):
                continue
            parsed = self._parse_line(line, index)
            if parsed:
                entries.extend(parsed)
            else:
                skipped.append((index, line))
        _link_reset_plans(entries)
        _flag_conflicting_entries(entries)
        return ParseResult(entries, skipped)

    @staticmethod
    def _tokenize(line: str) -> list[str]:
        tokens: list[str] = []
        index = 0
        length = len(line)
        while index < length:
            while index < length and line[index] in " \t":
                index += 1
            if index >= length:
                break
            token: list[str] = []
            quoted = False
            while index < length:
                char = line[index]
                if char == "\\":
                    slashes = 0
                    while index < length and line[index] == "\\":
                        slashes += 1
                        index += 1
                    if index < length and line[index] == '"':
                        token.append("\\" * (slashes // 2))
                        index += 1
                        if slashes % 2:
                            token.append('"')
                        else:
                            quoted = not quoted
                    else:
                        token.append("\\" * slashes)
                elif char == '"':
                    index += 1
                    if quoted and index < length and line[index] == '"':
                        token.append('"')
                        index += 1
                    else:
                        quoted = not quoted
                elif not quoted and char in " \t":
                    break
                else:
                    token.append(char)
                    index += 1
            tokens.append("".join(token))
        return tokens

    @classmethod
    def _split_commands(cls, tokens: list[str]) -> Iterable[list[str]]:
        command: list[str] = []
        for token in tokens:
            if token in cls._SEPARATORS:
                if command:
                    yield command
                command = []
            else:
                command.append(token)
        if command:
            yield command

    def _parse_line(self, line: str, line_number: int) -> list[RegistryEntry]:
        entries: list[RegistryEntry] = []
        for tokens in self._split_commands(self._tokenize(line)):
            if tokens[0].rsplit("\\", 1)[-1].casefold() not in self._COMMAND_NAMES:
                continue
            try:
                entries.extend(self._parse_reg_command(tokens, line, line_number))
            except _LineSyntaxError as exc:
                hive, path, _ = self._resolve_key(tokens[2]) if len(tokens) >= 3 else (0, "", "")
                entries.append(_syntax_error_entry(line, line_number, str(exc), hive=hive, path=path))
        return entries

    @staticmethod
    def _resolve_key(raw_path: str) -> tuple[int, str, str]:
        expanded = _expand_env_vars(raw_path).rstrip("\\")
        hive_name, _, sub_path = expanded.partition("\\")
        return HIVE_NAMES.get(hive_name.upper(), 0), sub_path, hive_name

    def _parse_reg_command(
        self, tokens: list[str], line: str, line_number: int
    ) -> list[RegistryEntry]:
        sub_command = tokens[1].casefold() if len(tokens) >= 2 else ""
        if sub_command in self._UNVERIFIED_OPERATIONS:
            return []
        operation = self._OPERATIONS.get(sub_command) if len(tokens) >= 3 else None
        if operation is None:
            raise _LineSyntaxError(f"'{' '.join(tokens[:2])}' is not a recognised command")

        key_path = tokens[2]
        value_name: Optional[str] = None
        type_str: Optional[str] = None
        data: Optional[str] = None
        separator: Optional[str] = None
        view: Optional[int] = None
        all_values = False
        default_value = False

        index = 3
        seen_switches: set[str] = set()
        argument_switches = (
            self._ADD_ARGUMENT_SWITCHES
            if operation is Operation.ADD
            else self._DELETE_ARGUMENT_SWITCHES
        )
        while index < len(tokens):
            token = tokens[index]
            switch = token.casefold()
            index += 1
            if switch in argument_switches:
                if switch in seen_switches:
                    raise _LineSyntaxError(f"duplicate switch '{token}'")
                seen_switches.add(switch)
                if index >= len(tokens):
                    raise _LineSyntaxError(f"switch '{token}' has no value")
                argument = tokens[index]
                index += 1
                if switch == "/v":
                    value_name = argument
                elif switch == "/t":
                    type_str = argument
                elif switch == "/s":
                    separator = argument
                else:
                    data = argument
            elif switch == "/ve":
                default_value = True
            elif switch == "/va":
                all_values = True
            elif switch in ("/reg:32", "/reg:64"):
                view = int(switch[-2:])
            elif self._REDIRECT.fullmatch(token):
                if token.endswith(">"):
                    index += 1
            elif switch != "/f":
                raise _LineSyntaxError(f"unsupported switch '{token}'")

        if default_value:
            if value_name is not None:
                raise _LineSyntaxError("'/ve' cannot be combined with '/v'")
            value_name = ""

        if all_values:
            if operation is not Operation.DELETE:
                raise _LineSyntaxError("'/va' is only valid for 'reg delete'")
            if value_name is not None:
                raise _LineSyntaxError("'/va' cannot be combined with '/v' or '/ve'")

        if operation is Operation.DELETE:
            return self._build_entries(
                key_path,
                value_name,
                ValueType.DELETE,
                "Deleted",
                Operation.DELETE,
                line_number,
                raw_command=line,
                view=view,
                all_values=all_values,
            )

        if data is None and value_name is None and type_str is None:
            return self._build_entries(
                key_path,
                None,
                ValueType.KEY,
                "Exists",
                Operation.ADD,
                line_number,
                raw_command=line,
                view=view,
            )
        value_type = self._safe_type(type_str) if type_str else ValueType.SZ
        if data is None:
            data = "0" if value_type in _INT_MASKS else ""
        if separator and value_type is ValueType.MULTI_SZ:
            data = data.replace(separator, "\\0")
        try:
            _prepare_value(data, value_type)
        except ValueError:
            raise _LineSyntaxError(f"'{data}' is not valid {value_type.value} data")
        return self._build_entries(
            key_path,
            value_name or "",
            value_type,
            data,
            Operation.ADD,
            line_number,
            raw_command=line,
            view=view,
        )

    def _build_entries(
        self,
        raw_path: str,
        value_name: Optional[str],
        value_type: ValueType,
        expected: Optional[str],
        operation: Operation,
        line_number: int,
        *,
        raw_command: str,
        view: Optional[int],
        all_values: bool = False,
    ) -> list[RegistryEntry]:
        hive_handle, sub_path, hive_name = self._resolve_key(raw_path)
        if not hive_handle:
            raise _LineSyntaxError(f"unknown registry hive '{hive_name}'")

        if operation == Operation.DELETE and value_name is None and not all_values and not sub_path:
            raise _LineSyntaxError(f"refusing to delete the whole {hive_name.upper()} hive")

        sub_paths = self._expand_sub_paths(hive_handle, sub_path)
        return [
            RegistryEntry(
                hive=hive_handle,
                path=resolved_path,
                value_name=value_name,
                value_type=value_type,
                expected=expected,
                operation=operation,
                source_line=line_number,
                raw_command=raw_command,
                view=view,
                all_values=all_values,
            )
            for resolved_path in sub_paths
        ]

    def _expand_sub_paths(self, hive: int, sub_path: str) -> list[str]:
        if not sub_path:
            return [""]
        if hive != winreg.HKEY_USERS:
            return [sub_path]

        first_component, separator, remainder = sub_path.partition("\\")
        if not any(char in first_component for char in ("*", "?")):
            return [sub_path]

        matches = _cached_hku_subkeys(first_component)
        suffix = f"{separator}{remainder}" if separator else ""
        return [f"{match}{suffix}" for match in matches]

    @staticmethod
    def _safe_type(raw: str) -> ValueType:
        with suppress(ValueError):
            value_type = ValueType(raw.upper())
            if value_type not in (ValueType.DELETE, ValueType.RESET, ValueType.KEY, ValueType.ERROR):
                return value_type
        raise _LineSyntaxError(f"unsupported value type '{raw}'")


def _format_actual_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, list):
        return "\\0".join(value)
    return str(value)


def _compare_expected(expected: str, actual: str, value_type: ValueType) -> bool:
    if value_type == ValueType.MULTI_SZ:
        expected_list = _split_multi_sz(expected)
        actual_list = _split_multi_sz(actual)
        return expected_list == actual_list
    if value_type == ValueType.BINARY:
        norm_expected = expected.replace(" ", "").lower()
        norm_actual = actual.replace(" ", "").lower()
        return norm_expected == norm_actual
    if value_type in _INT_MASKS:
        mask = _INT_MASKS[value_type]
        try:
            return _parse_reg_int(expected) & mask == _parse_reg_int(actual) & mask
        except ValueError:
            pass
    if value_type == ValueType.EXPAND_SZ:
        expanded_expected = _expand_env_vars(expected)
        expanded_actual = _expand_env_vars(actual)
        return os.path.normcase(expanded_expected) == os.path.normcase(expanded_actual)
    return expected == actual


def _split_multi_sz(value: str) -> list[str]:
    if not value:
        return [""]
    return value.replace("\\0", "\0").split("\0")


def _parse_reg_int(text: str) -> int:
    try:
        return int(text, 0)
    except ValueError:
        return int(text, 10)


def _prepare_value(data: str, value_type: ValueType) -> object:
    if value_type in _INT_MASKS:
        mask = _INT_MASKS[value_type]
        number = _parse_reg_int(data)
        if not -(mask // 2) - 1 <= number <= mask:
            raise ValueError(f"{data} is out of range for {value_type.value}")
        return number & mask
    if value_type == ValueType.BINARY:
        return bytes.fromhex(data.replace(" ", ""))
    if value_type == ValueType.MULTI_SZ:
        return _split_multi_sz(data)
    return data


class _CancellableWorker:
    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()


class RegistryInspector(_CancellableWorker):
    def scan(
        self,
        entries: Iterable[RegistryEntry],
        progress: Optional[Callable[[int], None]] = None,
    ) -> list[ScanResult]:
        groups: dict[tuple[int, str, int], list[RegistryEntry]] = defaultdict(list)
        for entry in entries:
            groups[(entry.hive, entry.path.casefold(), _VIEW_INDEX[entry.view])].append(entry)

        results: list[ScanResult] = []
        max_workers = max(1, min(MAX_SCAN_WORKERS, len(groups)))
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            pending = {
                executor.submit(self._scan_group, group) for group in groups.values()
            }

            while pending:
                if self._cancelled.is_set():
                    for future in pending:
                        future.cancel()
                    raise CancelledError()
                done, pending = wait(pending, timeout=0.3, return_when=FIRST_COMPLETED)
                for future in done:
                    group_results = future.result()
                    results.extend(group_results)
                    if progress:
                        progress(len(group_results))

        return results

    def _scan_group(self, entries: list[RegistryEntry]) -> list[ScanResult]:
        first = entries[0]
        hive, path, view = first.hive, first.path, first.view
        _raise_if_cancelled(self._cancelled)
        result = _open_key_in_view(hive, path, *_view_option(READ_VIEW_OPTIONS, view))
        if result.handle:
            return self._scan_with_handle(entries, result.handle, result.label, result.access)

        if result.denied:
            denied = [
                ScanResult(entry.unique_id, None, False, "Access denied", access_denied=True)
                for entry in entries
            ]
            _annotate_results(denied, result.denied)
            return denied

        if result.error is not None:
            return [
                ScanResult(entry.unique_id, None, False, f"Read failed: {result.error}")
                for entry in entries
            ]

        virt_handle = _open_virtual_store_key(hive, path, winreg.KEY_READ)
        if virt_handle:
            return self._scan_with_handle(entries, virt_handle, "VirtualStore", winreg.KEY_READ)

        missing_root = _hive_root_missing(hive, path)
        if missing_root:
            return [self._unloaded_root_result(entry, missing_root) for entry in entries]

        return [self._missing_entry_result(entry) for entry in entries]

    def _scan_with_handle(
        self,
        entries: list[RegistryEntry],
        handle: Any,
        label: Optional[str],
        access: int,
    ) -> list[ScanResult]:
        try:
            results = [self._scan_entry(entry, handle, access) for entry in entries]
        finally:
            handle.Close()
        _annotate_results(results, label)
        return results

    @staticmethod
    def _unloaded_root_result(entry: RegistryEntry, root: str) -> ScanResult:
        if entry.operation == Operation.DELETE:
            return ScanResult(entry.unique_id, actual=None, compliant=True, detail="Compliant")
        return ScanResult(
            entry.unique_id,
            actual=None,
            compliant=None,
            detail=f"Not applicable: '{root}' does not exist under {entry.hive_name} and cannot be created",
        )

    @staticmethod
    def _missing_entry_result(entry: RegistryEntry) -> ScanResult:
        if entry.operation == Operation.DELETE:
            detail = "Compliant: key does not exist" if entry.is_reset else "Compliant"
            return ScanResult(entry.unique_id, actual=None, compliant=True, detail=detail)
        return ScanResult(entry.unique_id, actual=None, compliant=False, detail="Key not found")

    def _scan_entry(self, entry: RegistryEntry, handle, access: int) -> ScanResult:
        _raise_if_cancelled(self._cancelled)

        if entry.operation == Operation.DELETE:
            return self._scan_delete(entry, handle, access)
        return self._scan_add(entry, handle)

    @classmethod
    def _scan_delete(cls, entry: RegistryEntry, handle, access: int) -> ScanResult:
        if entry.all_values:
            return cls._scan_all_values(entry, handle)
        if entry.value_name is None:
            if entry.reset_plan is not None:
                return cls._scan_reset(entry, entry.reset_plan, handle, access)
            return ScanResult(entry.unique_id, "Key exists", False, "Key should be deleted")
        try:
            value, _ = winreg.QueryValueEx(handle, entry.value_name)
        except FileNotFoundError:
            return ScanResult(entry.unique_id, actual=None, compliant=True, detail="Compliant")
        except PermissionError:
            return ScanResult(
                entry.unique_id, actual=None, compliant=False, detail="Access denied", access_denied=True
            )
        except OSError as exc:
            return ScanResult(entry.unique_id, actual=None, compliant=False, detail=f"Read failed: {exc}")
        actual = _format_actual_value(value)
        return ScanResult(
            entry.unique_id,
            actual=actual,
            compliant=False,
            detail=f"Value exists: {actual}",
        )

    @staticmethod
    def _scan_all_values(entry: RegistryEntry, handle) -> ScanResult:
        plan = entry.reset_plan
        allowed = plan.values.get("", frozenset()) if plan else frozenset()
        extras = [
            name for name in _enumerate_names(handle, winreg.EnumValue)
            if name.casefold() not in allowed
        ]
        if not extras:
            detail = (
                f"Compliant: key holds only the {plan.declared_count} listed value(s)"
                if plan
                else "Compliant"
            )
            return ScanResult(
                entry.unique_id,
                actual=RESET_EXPECTED_TEXT if plan else None,
                compliant=True,
                detail=detail,
            )
        shown = ", ".join(_value_label(name) for name in extras[:RESET_EXTRA_DISPLAY])
        remainder = "…" if len(extras) > RESET_EXTRA_DISPLAY else ""
        return ScanResult(
            entry.unique_id,
            actual=f"{len(extras)} value(s)",
            compliant=False,
            detail=f"Values should be deleted: {shown}{remainder}",
        )

    @classmethod
    def _scan_reset(
        cls,
        entry: RegistryEntry,
        plan: ResetPlan,
        handle,
        access: int,
    ) -> ScanResult:
        extras, truncated = cls._collect_reset_extras(handle, access, plan)
        if truncated and not extras:
            return ScanResult(
                entry.unique_id,
                actual=None,
                compliant=None,
                detail=f"Key too large to verify, stopped after {RESET_MAX_NODES} entries",
            )
        if not extras:
            return ScanResult(
                entry.unique_id,
                actual=RESET_EXPECTED_TEXT,
                compliant=True,
                detail=f"Compliant: key holds only the {plan.declared_count} listed entry(s)",
            )
        shown = ", ".join(extras[:RESET_EXTRA_DISPLAY])
        remainder = "…" if truncated or len(extras) > RESET_EXTRA_DISPLAY else ""
        return ScanResult(
            entry.unique_id,
            actual="Unlisted content",
            compliant=False,
            detail=f"Key must be reset, holds {shown}{remainder}",
        )

    @classmethod
    def _collect_reset_extras(
        cls,
        root_handle,
        access: int,
        plan: ResetPlan,
    ) -> tuple[list[str], bool]:
        extras: list[str] = []
        inspected = 0
        pending: list[tuple[Any, str, str]] = [(root_handle, "", "")]
        opened: list[Any] = []
        try:
            while pending:
                handle, relative, shown = pending.pop()
                allowed = plan.values.get(relative, frozenset())
                for name in _enumerate_names(handle, winreg.EnumValue):
                    inspected += 1
                    if name.casefold() not in allowed:
                        extras.append(cls._extra_label("value", shown, name))
                    if len(extras) >= RESET_EXTRA_LIMIT or inspected >= RESET_MAX_NODES:
                        return extras, True
                for sub_key in _enumerate_names(handle, winreg.EnumKey):
                    inspected += 1
                    child = _join_sub_path(relative, sub_key.casefold())
                    if child not in plan.keys:
                        extras.append(cls._extra_label("key", shown, sub_key))
                        if len(extras) >= RESET_EXTRA_LIMIT or inspected >= RESET_MAX_NODES:
                            return extras, True
                        continue
                    try:
                        child_handle = winreg.OpenKey(handle, sub_key, 0, access)
                    except OSError as exc:
                        logger.info("Reset scan could not open %s: %s", child, exc)
                        continue
                    opened.append(child_handle)
                    pending.append((child_handle, child, _join_sub_path(shown, sub_key)))
        finally:
            for handle in opened:
                with suppress(OSError):
                    handle.Close()
        return extras, False

    @staticmethod
    def _extra_label(kind: str, relative: str, name: str) -> str:
        location = _join_sub_path(relative, name if kind == "key" else _value_label(name))
        return f"{kind} '{location}'"

    @staticmethod
    def _scan_add(entry: RegistryEntry, handle) -> ScanResult:
        if entry.value_name is None:
            return ScanResult(entry.unique_id, "Key exists", True, "Compliant")
        try:
            value, reg_type = winreg.QueryValueEx(handle, entry.value_name)
        except FileNotFoundError:
            return ScanResult(entry.unique_id, actual=None, compliant=False, detail="Value not found")
        except PermissionError:
            return ScanResult(
                entry.unique_id, actual=None, compliant=False, detail="Access denied", access_denied=True
            )
        except OSError as exc:
            return ScanResult(entry.unique_id, actual=None, compliant=False, detail=f"Read failed: {exc}")
        actual = _format_actual_value(value)
        if reg_type != _WINREG_TYPE_MAP[entry.value_type]:
            found = _REG_TYPE_NAMES.get(reg_type, f"type {reg_type}")
            return ScanResult(
                entry.unique_id,
                actual=actual,
                compliant=False,
                detail=_type_mismatch_detail(entry.value_type.value, found),
                type_mismatch=True,
            )
        compliant = _compare_expected(entry.expected_text, actual, entry.value_type)
        if compliant:
            detail = "Compliant"
        else:
            shown_expected = _format_reg_int_display(entry.expected_text, entry.value_type)
            shown_actual = _format_reg_int_display(actual, entry.value_type)
            detail = f"Expected '{shown_expected}' got '{shown_actual}'"
        return ScanResult(entry.unique_id, actual=actual, compliant=compliant, detail=detail)


class RegistryApplier(_CancellableWorker):
    def execute(
        self,
        entries: Iterable[RegistryEntry],
        progress: Optional[Callable[[int], None]] = None,
    ) -> ExecutionOutcome:
        successes = 0
        failures = 0
        skipped = 0
        errors: list[str] = []
        denied_ids: list[str] = []

        for entry in entries:
            _raise_if_cancelled(self._cancelled)
            try:
                if entry.operation == Operation.ADD:
                    self._apply_add(entry)
                else:
                    self._apply_delete(entry)
                successes += 1
            except CancelledError:
                raise
            except HiveNotLoadedError as exc:
                skipped += 1
                logger.info(
                    "Skipped %s::%s -> %s", entry.full_path, entry.display_name, exc
                )
            except Exception as exc:
                failures += 1
                message = f"{entry.full_path}::{entry.display_name} -> {exc}"
                errors.append(message)
                if isinstance(exc, PermissionError):
                    denied_ids.append(entry.unique_id)
                else:
                    logger.error("Execution error: %s", message)
            finally:
                if progress:
                    progress(1)
        return ExecutionOutcome(successes, failures, errors, skipped, denied_ids)

    def _apply_add(self, entry: RegistryEntry) -> None:
        if entry.value_name is None:
            self._open_or_create(entry.hive, entry.path, entry.view).Close()
            return
        if entry.expected is None:
            raise ValueError("ADD entry missing expected value")
        data = _prepare_value(entry.expected, entry.value_type)
        with self._open_or_create(entry.hive, entry.path, entry.view) as handle:
            winreg.SetValueEx(handle, entry.value_name, 0, _WINREG_TYPE_MAP[entry.value_type], data)

    def _apply_delete(self, entry: RegistryEntry) -> None:
        if entry.all_values:
            self._delete_all_values(entry.hive, entry.path, entry.view)
            return
        if entry.value_name is None:
            self._delete_key_recursive(entry.hive, entry.path, entry.view)
            return
        deleted = self._delete_value(entry.hive, entry.path, entry.value_name, entry.view)
        if not deleted:
            logger.info("Value already absent: %s\\%s", entry.full_path, entry.display_name)

    def _open_or_create(self, hive: int, path: str, view: Optional[int] = None):
        access = _view_option(WRITE_VIEW_FLAGS, view)
        failures: list[tuple[str, OSError]] = []
        for opener in (winreg.OpenKey, winreg.CreateKeyEx):
            try:
                return opener(hive, path, 0, access)
            except PermissionError:
                raise
            except OSError as exc:
                failures.append((opener.__name__, exc))

        missing_root = _hive_root_missing(hive, path)
        if missing_root:
            raise HiveNotLoadedError(hive, missing_root)

        detail = "; ".join(dict.fromkeys(f"{name}: {exc}" for name, exc in failures))
        raise OSError(f"cannot open or create {path!r} (access=0x{access:08x}) [{detail}]")

    def _delete_key_recursive(self, hive: int, path: str, view: Optional[int] = None) -> None:
        if not path.strip("\\"):
            raise ValueError("refusing to recursively delete a registry hive root")
        view_flag, write_flag = _view_option(DELETE_VIEW_FLAGS, view)
        self._delete_branch_with_access(hive, path, write_flag | REG_BRANCH_ACCESS, view_flag)
        virt = _virtualized_location(hive, path)
        if virt:
            self._delete_branch_with_access(
                virt[0], virt[1], winreg.KEY_SET_VALUE | REG_BRANCH_ACCESS, None
            )

    def _clear_registry_branch(self, handle: Any, recurse: Callable[[str], None]) -> None:
        for sub_key in _enumerate_names(handle, winreg.EnumKey):
            recurse(sub_key)
        for value_name in _enumerate_names(handle, winreg.EnumValue):
            winreg.DeleteValue(handle, value_name)

    def _delete_branch_with_access(
        self,
        hive: int,
        path: str,
        access: int,
        view_flag: Optional[int],
    ) -> None:
        try:
            handle = winreg.OpenKey(hive, path, 0, access)
        except FileNotFoundError:
            return
        with handle:
            self._clear_registry_branch(
                handle,
                lambda sub: self._delete_branch_with_access(hive, _join_sub_path(path, sub), access, view_flag),
            )
        with suppress(FileNotFoundError):
            if view_flag is None:
                winreg.DeleteKey(hive, path)
            else:
                winreg.DeleteKeyEx(hive, path, view_flag, 0)

    @staticmethod
    def _value_locations(hive: int, path: str, view: Optional[int]) -> list[tuple[int, str, int]]:
        locations = [(hive, path, _view_option(WRITE_VIEW_FLAGS, view))]
        virt = _virtualized_location(hive, path)
        if virt:
            locations.append((virt[0], virt[1], winreg.KEY_SET_VALUE))
        return locations

    def _delete_value(self, hive: int, path: str, value_name: str, view: Optional[int] = None) -> bool:
        deleted = False
        for target_hive, target_path, access in self._value_locations(hive, path, view):
            try:
                with winreg.OpenKey(target_hive, target_path, 0, access) as handle:
                    winreg.DeleteValue(handle, value_name)
                    deleted = True
            except FileNotFoundError:
                continue
        return deleted

    def _delete_all_values(self, hive: int, path: str, view: Optional[int] = None) -> None:
        for target_hive, target_path, access in self._value_locations(hive, path, view):
            try:
                handle = winreg.OpenKey(target_hive, target_path, 0, access | winreg.KEY_QUERY_VALUE)
            except FileNotFoundError:
                continue
            with handle:
                for value_name in _enumerate_names(handle, winreg.EnumValue):
                    winreg.DeleteValue(handle, value_name)


class _WorkerBase(QThread):
    completed = pyqtSignal(object)
    progress_changed = pyqtSignal(int, int)
    failed = pyqtSignal(str)
    CANCEL_MESSAGE = "Operation cancelled"
    LOG_LABEL = "Worker"

    def __init__(self, entries: list[RegistryEntry], engine: _CancellableWorker) -> None:
        super().__init__()
        self.entries = entries
        self._engine = engine

    def run(self) -> None:
        total = len(self.entries)
        processed = 0
        last_emit = 0.0

        def on_progress(count: int) -> None:
            nonlocal processed, last_emit
            processed += count
            now = time.monotonic()
            if processed >= total or now - last_emit >= PROGRESS_EMIT_INTERVAL_S:
                last_emit = now
                self.progress_changed.emit(processed, total)

        try:
            self.completed.emit(self._execute(self.entries, on_progress))
        except CancelledError:
            self.failed.emit(self.CANCEL_MESSAGE)
        except Exception as exc:
            logger.exception("%s failed", self.LOG_LABEL)
            self.failed.emit(str(exc))

    def cancel(self) -> None:
        self._engine.cancel()

    def _execute(
        self,
        entries: Sequence[RegistryEntry],
        progress: Callable[[int], None],
    ) -> Any:
        raise NotImplementedError


class ScanWorker(_WorkerBase):
    CANCEL_MESSAGE = "Scan cancelled"
    LOG_LABEL = "Scan"

    def __init__(self, entries: list[RegistryEntry]) -> None:
        super().__init__(entries, RegistryInspector())

    def _execute(
        self,
        entries: Sequence[RegistryEntry],
        progress: Callable[[int], None],
    ) -> list[ScanResult]:
        return self._engine.scan(entries, progress)


class ApplyWorker(_WorkerBase):
    CANCEL_MESSAGE = "Execution cancelled"
    LOG_LABEL = "Execution"

    def __init__(self, entries: list[RegistryEntry]) -> None:
        super().__init__(entries, RegistryApplier())

    def _execute(
        self,
        entries: Sequence[RegistryEntry],
        progress: Callable[[int], None],
    ) -> ExecutionOutcome:
        return self._engine.execute(entries, progress)


FONT_SEGOE = QFont("Segoe UI", 9)
FONT_SEGOE_BOLD_8 = QFont("Segoe UI", 8, QFont.Weight.Bold)

STATIC_ITEM_FLAGS = Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled
CHECKABLE_ITEM_FLAGS = STATIC_ITEM_FLAGS | Qt.ItemFlag.ItemIsUserCheckable

MAX_ERROR_DISPLAY = 10
MAX_SKIPPED_DISPLAY = 25


@dataclass(slots=True)
class ActionSpec:
    icon: QStyle.StandardPixmap
    text: str
    handler: Callable[[], None]
    attr: str = ""
    shortcut: Optional[Union[QKeySequence, str]] = None
    role: str = "secondary"
    tooltip: str = ""


COLUMN_CONFIG: tuple[tuple[str, int], ...] = (
    ("UID", 64),
    ("Select", 60),
    ("Path", 480),
    ("Value", 190),
    ("Type", 95),
    ("Expected", 190),
    ("Actual", 155),
    ("Details", 322),
)
TABLE_HEADERS: list[str] = [name for name, _ in COLUMN_CONFIG]
DEFAULT_COLUMN_WIDTHS: dict[str, int] = {name: width for name, width in COLUMN_CONFIG}
UID_COLUMN, SELECT_COLUMN, PATH_COLUMN, VALUE_COLUMN, TYPE_COLUMN, EXPECTED_COLUMN, ACTUAL_COLUMN, DETAILS_COLUMN = range(len(TABLE_HEADERS))

DEFAULT_WINDOW_SIZE: tuple[int, int] = (1504, 722)

STATUS_STYLE_BASE = "font-size: 18px; font-weight: 700;"
STATUS_STYLES: dict[str, tuple[str, Optional[str], str]] = {
    "ready":   ("color: #38bdf8;", None,      "●"),
    "success": ("color: #34d399;", None,      "✔"),
    "working": ("color: #facc15;", "#facc15", "●"),
    "error":   ("color: #f87171;", None,      "✖"),
}

MISMATCH_STATE = "mismatch"
MISMATCH_COLOR = "#ff2020"

STATE_CONFIG: dict[str, dict[str, Any]] = {
    MISMATCH_STATE: {
        "color": MISMATCH_COLOR,
        "label": "List Error ({count})",
    },
    "compliant": {
        "color": "#34d399",
        "default": False,
        "label": "Compliant ({count})",
        "compliance_value": True,
    },
    "noncompliant": {
        "color": "#f87171",
        "default": True,
        "label": "Non-Compliant ({count})",
        "compliance_value": False,
    },
    "pending": {
        "color": "#facc15",
        "default": True,
        "label": "Pending ({count})",
        "compliance_value": None,
    },
}

TOGGLE_STATES: tuple[str, ...] = tuple(
    state for state, cfg in STATE_CONFIG.items() if "compliance_value" in cfg
)
COMPLIANCE_TO_STATE: dict[Optional[bool], str] = {
    STATE_CONFIG[state]["compliance_value"]: state for state in TOGGLE_STATES
}
COMPLIANCE_ORDER: dict[str, int] = {state: i for i, state in enumerate(STATE_CONFIG)}
COMPLIANCE_TEXT: dict[Optional[bool], str] = {True: "Yes", False: "No", None: "Pending"}

OPERATION_FILTERS: tuple[tuple[Operation, str], ...] = (
    (Operation.ADD, "Show “reg add” entries"),
    (Operation.DELETE, "Show “reg delete” entries"),
)
DEFAULT_OPERATION_VISIBILITY: dict[Operation, bool] = {operation: True for operation, _ in OPERATION_FILTERS}
OPERATION_ONLY_LABELS: dict[Operation, str] = {
    Operation.ADD: "Type (add)",
    Operation.DELETE: "Type (del)",
}
TYPE_HEADER_TOOLTIP = "Right-click for “reg add” / “reg delete” filters"
LAST_OPERATION_TOOLTIP = "At least one command type must stay visible"

TOGGLE_OFF_COLOR = "#1f2937"
STATUS_MESSAGE_WIDTH = 200
TOOLBAR_BUTTON_PADDING = 24
TOOLBAR_BUTTON_WIDTH_PRIMARY = 112
TOOLBAR_BUTTON_WIDTH_SECONDARY = 99
WORKER_WAIT_TIMEOUT_MS = 1000
PROGRESS_RESET_DELAY_MS = 2000
SETTINGS_SAVE_DELAY_MS = 750

SUPPORTED_LIST_EXTENSIONS = frozenset((".txt", ".bat", ".cmd"))
SUPPORTED_LIST_DESC = ", ".join(sorted(SUPPORTED_LIST_EXTENSIONS))
SUPPORTED_LIST_PATTERN = " ".join(f"*{ext}" for ext in sorted(SUPPORTED_LIST_EXTENSIONS))
SUPPORTED_LIST_FILTER = f"Registry lists ({SUPPORTED_LIST_PATTERN});;All files (*.*)"
DEFAULT_LIST_BASENAME = "List"

HELP_TEXT = (
    "<b>Registry Sentinel</b><br><br>"
    "1. Load a registry command list (List.txt).<br>"
    "2. Click <b>Scan</b> to evaluate compliance.<br>"
    "3. Tick entries to fix, then choose <b>Apply Selected</b>.<br>"
    "4. Export a CSV report, or jump to a key in Regedit from the row context menu.<br><br>"
    "A <b>reg delete</b> of a key followed by <b>reg add</b> lines for that same key is read as a "
    "<b>RESET</b>: it is compliant while the key holds nothing but the values the list adds, and "
    "applying it rewrites those values afterwards.<br>"
    "Right-click the column header to filter by <b>reg add</b> / <b>reg delete</b>; the choice is remembered."
)


STYLESHEET = """
QMainWindow {
    background: #0f172a;
    color: #e2e8f0;
}

QToolBar {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:0,
        stop:0 #1e293b, stop:0.5 #2563eb, stop:1 #1e293b);
    border: none;
    padding: 6px;
}

QToolBar QToolButton {
    color: #e2e8f0;
    background-color: rgba(148, 163, 184, 0.18);
    border-radius: 6px;
    padding: 6px 10px;
    margin: 0 4px;
}

QToolBar QToolButton:hover {
    background-color: rgba(59, 130, 246, 0.45);
}

QToolBar QToolButton:pressed {
    background-color: rgba(59, 130, 246, 0.6);
}

QToolBar QToolButton[colorRole="primary"] {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #3b82f6, stop:1 #2563eb);
    color: #f8fafc;
    border: 1px solid rgba(37, 99, 235, 0.55);
}

QToolBar QToolButton[colorRole="primary"]:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #60a5fa, stop:1 #3b82f6);
}

QToolBar QToolButton[colorRole="primary"]:pressed {
    background-color: #1d4ed8;
}

QToolBar QToolButton[colorRole="success"] {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #34d399, stop:1 #059669);
    color: #f8fafc;
    border: 1px solid rgba(15, 118, 110, 0.55);
}

QToolBar QToolButton[colorRole="success"]:disabled {
    background-color: rgba(15, 118, 110, 0.28);
    color: rgba(226, 232, 240, 0.55);
    border: 1px solid rgba(15, 118, 110, 0.22);
}

QToolBar QToolButton[colorRole="success"]:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #6ee7b7, stop:1 #34d399);
}

QToolBar QToolButton[colorRole="success"]:pressed {
    background-color: #047857;
}

QToolBar QToolButton[colorRole="warning"] {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #fbbf24, stop:1 #f59e0b);
    color: #0f172a;
    border: 1px solid rgba(217, 119, 6, 0.6);
}

QToolBar QToolButton[colorRole="warning"]:hover {
    background-color: qlineargradient(x1:0, y1:0, x2:0, y2:1,
        stop:0 #fcd34d, stop:1 #fbbf24);
}

QToolBar QToolButton[colorRole="warning"]:pressed {
    background-color: #d97706;
}

QToolBar QToolButton[colorRole="secondary"] {
    background-color: rgba(148, 163, 184, 0.22);
    color: #e2e8f0;
    border: 1px solid rgba(148, 163, 184, 0.35);
}

QToolBar QToolButton[colorRole="secondary"]:hover {
    background-color: rgba(148, 163, 184, 0.32);
}

QToolBar QToolButton[colorRole="secondary"]:pressed {
    background-color: rgba(100, 116, 139, 0.45);
}

QTableWidget {
    background-color: rgba(15, 23, 42, 0.92);
    alternate-background-color: rgba(30, 41, 59, 0.92);
    gridline-color: #1f2937;
    color: #cbd5f5;
    selection-background-color: rgba(59, 130, 246, 0.55);
    selection-color: #f8fafc;
    border-radius: 10px;
    border: 1px solid rgba(148, 163, 184, 0.25);
}

QTableWidget::item:hover {
    background-color: rgba(59, 130, 246, 0.10);
}

QHeaderView::section {
    background-color: rgba(30, 41, 59, 0.95);
    color: #94a3b8;
    padding: 8px 10px;
    border: none;
    border-right: 1px solid rgba(71, 85, 105, 0.6);
    border-bottom: 1px solid rgba(71, 85, 105, 0.6);
}

QHeaderView::section:sort-up,
QHeaderView::section:sort-down {
    color: #f8fafc;
    font-weight: 600;
}

QHeaderView::section:hover {
    background-color: rgba(71, 85, 105, 0.55);
    color: #e2e8f0;
}

QLineEdit {
    background-color: rgba(15, 23, 42, 0.8);
    border-radius: 8px;
    border: 1px solid rgba(148, 163, 184, 0.35);
    padding: 6px 12px;
    color: #e2e8f0;
}

QLineEdit:focus {
    border-color: #3b82f6;
}

QCheckBox {
    color: #cbd5f5;
}

QCheckBox::indicator,
QTableWidget::indicator {
    width: 18px;
    height: 18px;
    border-radius: 5px;
    border: 1px solid rgba(148, 163, 184, 0.35);
    background: rgba(15, 23, 42, 0.9);
}

QCheckBox::indicator:checked,
QTableWidget::indicator:checked {
    background-color: #2563eb;
    border-color: #2563eb;
}

QProgressBar {
    background-color: rgba(15, 23, 42, 0.85);
    border-radius: 8px;
    border: 1px solid rgba(59, 130, 246, 0.4);
    color: #e2e8f0;
    text-align: center;
}

QProgressBar::chunk {
    background: qlineargradient(x1:0, x2:1, stop:0 #2563eb, stop:1 #9333ea);
    border-radius: 7px;
}

QMenu {
    background-color: #0f172a;
    border: 1px solid rgba(148, 163, 184, 0.35);
    color: #e2e8f0;
    border-radius: 8px;
    padding: 4px;
}

QMenu::item {
    padding: 6px 14px;
    border-radius: 4px;
}

QMenu::item:selected {
    background-color: rgba(59, 130, 246, 0.35);
}

QMenu::separator {
    height: 1px;
    background: rgba(148, 163, 184, 0.2);
    margin: 4px 8px;
}

QToolTip {
    background-color: rgba(15, 23, 42, 0.95);
    color: #f8fafc;
    border: 1px solid rgba(148, 163, 184, 0.3);
    padding: 6px;
    border-radius: 6px;
    font-size: 11px;
}

QFrame#inlineFindPanel {
    background: rgba(15, 23, 42, 0.9);
    border: 1px solid rgba(148, 163, 184, 0.35);
    border-radius: 14px;
    padding: 10px 16px;
}

QLineEdit#inlineFindInput {
    background: rgba(30, 41, 59, 0.55);
    border: 1px solid rgba(148, 163, 184, 0.25);
    border-radius: 10px;
    color: #f8fafc;
    padding: 6px 12px;
    padding-right: 28px;
    min-width: 260px;
    selection-background-color: rgba(59, 130, 246, 0.55);
}

QLabel#inlineCount {
    background: rgba(30, 64, 175, 0.35);
    border-radius: 10px;
    color: #c7d2fe;
    font-size: 12px;
    padding: 4px 10px;
    min-width: 54px;
    qproperty-alignment: 'AlignCenter';
}

QToolButton#inlineFindNavButton {
    background: rgba(148, 163, 184, 0.24);
    border: 1px solid rgba(148, 163, 184, 0.45);
    border-radius: 12px;
    padding: 6px 18px;
    color: #e2e8f0;
    font-weight: 600;
    letter-spacing: 0.2px;
}

QToolButton#inlineFindNavButton:hover {
    background: rgba(96, 165, 250, 0.32);
    border-color: rgba(148, 163, 184, 0.65);
}

QToolButton#inlineFindNavButton:pressed {
    background: rgba(59, 130, 246, 0.54);
    border-color: rgba(59, 130, 246, 0.8);
}

QToolButton#inlineFindClearButton {
    background: rgba(59, 130, 246, 0.82);
    border: none;
    border-radius: 12px;
    padding: 6px 18px;
    color: #0f172a;
    font-weight: 600;
}

QToolButton#inlineFindClearButton:hover {
    background: rgba(96, 165, 250, 0.92);
}

QToolButton#inlineFindClearButton:pressed {
    background: rgba(37, 99, 235, 0.95);
}

QFrame#footerBar {
    background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 #1e293b, stop:1 #312e81);
    border-top: 1px solid rgba(100, 116, 139, 0.4);
    padding: 10px 14px;
}

QFrame#statusContainer {
    background: rgba(30, 41, 59, 0.7);
    border: 1px solid rgba(148, 163, 184, 0.25);
    border-radius: 14px;
    padding: 10px 14px;
}

QLabel#statusText {
    color: #e0f2fe;
    font-size: 12px;
}

QLabel#timerText {
    color: rgba(148, 163, 184, 0.78);
    font-size: 11px;
}

QLabel#footerEntries {
    color: #38bdf8;
    font-size: 12px;
    font-weight: 600;
}

QLabel#findIcon {
    font-size: 16px;
    color: #a5b4fc;
}

QProgressBar#footerProgress {
    background: rgba(15, 23, 42, 0.85);
    border-radius: 12px;
    border: 1px solid rgba(59, 130, 246, 0.4);
}

QProgressBar#footerProgress::chunk {
    background: qlineargradient(x1:0, x2:1, stop:0 #2563eb, stop:1 #a855f7);
    border-radius: 11px;
}

"""


class StyledHeader(QHeaderView):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(Qt.Orientation.Horizontal, parent)
        self.setSectionsClickable(True)
        self.setSortIndicatorShown(False)

    def paintSection(self, painter: QPainter, rect: QRect, logicalIndex: int) -> None:
        super().paintSection(painter, rect, logicalIndex)

        if self.sortIndicatorSection() != logicalIndex:
            return

        arrow_height = max(6, min(rect.height() - 6, 12))
        arrow_width = arrow_height
        half_width = arrow_width // 2
        base_x = rect.right() - max(10, arrow_width + 4)
        center_y = rect.center().y()

        arrow_color = QColor("#60a5fa") if self.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder else QColor("#a78bfa")
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(arrow_color)
        painter.setPen(Qt.PenStyle.NoPen)

        if self.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder:
            points = QPolygon(
                [
                    QPoint(base_x - half_width, center_y + arrow_height // 2),
                    QPoint(base_x, center_y - arrow_height // 2),
                    QPoint(base_x + half_width, center_y + arrow_height // 2),
                ]
            )
        else:
            points = QPolygon(
                [
                    QPoint(base_x - half_width, center_y - arrow_height // 2),
                    QPoint(base_x + half_width, center_y - arrow_height // 2),
                    QPoint(base_x, center_y + arrow_height // 2),
                ]
            )

        painter.drawPolygon(points)
        painter.restore()


class SentinelTable(QTableWidget):
    def setSortingEnabled(self, enable: bool) -> None:
        super().setSortingEnabled(enable)
        self.horizontalHeader().setSortIndicatorShown(False)

    def _visible_row(self, start: int, stop: int, step: int) -> Optional[int]:
        for row in range(start, stop, step):
            if not self.isRowHidden(row):
                return row
        return None

    def first_item_in_row(
        self,
        row: int,
        preferred_col: int = 0,
    ) -> tuple[Optional[QTableWidgetItem], int]:
        if not (0 <= row < self.rowCount()) or not self.columnCount():
            return None, max(preferred_col, 0)
        column = preferred_col if 0 <= preferred_col < self.columnCount() else 0
        item = self.item(row, column)
        if item is not None:
            return item, column
        for col in range(self.columnCount()):
            if col == column:
                continue
            candidate = self.item(row, col)
            if candidate is not None:
                return candidate, col
        return None, column

    def keyPressEvent(self, event) -> None:
        key = event.key()
        if key in (Qt.Key.Key_Home, Qt.Key.Key_End) and self.rowCount():
            if key == Qt.Key.Key_Home:
                target_row = self._visible_row(0, self.rowCount(), 1)
            else:
                target_row = self._visible_row(self.rowCount() - 1, -1, -1)

            if target_row is not None:
                target_col = self.currentColumn()
                if target_col < 0:
                    target_col = 0

                item, target_col = self.first_item_in_row(target_row, target_col)

                if item is not None:
                    self.setCurrentItem(item)
                    self.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
                else:
                    self.setCurrentCell(target_row, target_col)
                event.accept()
                return

        super().keyPressEvent(event)


SortValue = Union[str, int, tuple[Any, ...]]


class SentinelItem(QTableWidgetItem):
    def __init__(
        self,
        text: str,
        *,
        sort_value: Optional[SortValue] = None,
        sort_tiebreak: Sequence[Any] = (),
        pinned: bool = False,
    ) -> None:
        super().__init__(text)
        self._sort_value: SortValue = text.casefold() if sort_value is None else sort_value
        self._sort_tiebreak: tuple[Any, ...] = tuple(sort_tiebreak)
        self._pinned = pinned

    def set_sort_value(self, value: SortValue) -> None:
        self._sort_value = value

    def set_pinned(self, pinned: bool) -> None:
        self._pinned = pinned

    def _sorted_descending(self) -> bool:
        table = self.tableWidget()
        if table is None:
            return False
        return table.horizontalHeader().sortIndicatorOrder() == Qt.SortOrder.DescendingOrder

    def __lt__(self, other: "SentinelItem") -> bool:
        if self._pinned != other._pinned:
            return self._pinned != self._sorted_descending()

        if self._sort_value != other._sort_value:
            return self._sort_value < other._sort_value

        return self._sort_tiebreak < other._sort_tiebreak


class ToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

    def __init__(
        self,
        text: str = "",
        *,
        color_on: str = "#34d399",
        color_off: str = "#1f2937",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._color_on = QColor(color_on)
        self._color_off = QColor(color_off)
        self._color_disabled = QColor("#64748b")
        self._thumb_color = QColor("#f8fafc")
        self._checked = False
        self._position = 0.0
        self._hover = False

        self._font = FONT_SEGOE_BOLD_8
        self._switch_width = 42
        self._padding = 6
        self._tail_padding = 18
        self._switch_margin = 4
        self._thumb_margin = 3
        self._shadow_offset = 1
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        self._animation = QPropertyAnimation(self, b"position")
        self._animation.setDuration(200)
        self._animation.setEasingCurve(QEasingCurve.Type.InOutCubic)
        self._animation.finished.connect(self.update)

        self._min_width = 0
        self.setFixedHeight(24)
        self._update_size()

    def _base_width(self) -> int:
        return 2 * self._padding + self._switch_width

    def _update_size(self) -> None:
        width = self._calc_width()
        if width > self._min_width:
            self._min_width = width
        self.setFixedWidth(self._min_width)
        self.updateGeometry()

    def _calc_width(self) -> int:
        base = self._base_width()
        if not self._text:
            return base
        fm = QFontMetrics(self._font)
        return base + self._padding + fm.horizontalAdvance(self._text) + self._tail_padding

    def _get_position(self) -> float:
        return self._position

    def _set_position(self, value: float) -> None:
        self._position = value
        self.update()

    position = pyqtProperty(float, fget=_get_position, fset=_set_position)

    def enterEvent(self, event: QEvent) -> None:
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event: QEvent) -> None:
        if (
            isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.LeftButton
        ):
            self.toggle()
        super().mousePressEvent(event)

    def paintEvent(self, event: QEvent) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        switch_height = self.height() - self._switch_margin
        switch_x = self._padding
        switch_y = (self.height() - switch_height) // 2
        track = QRect(switch_x, switch_y, self._switch_width, switch_height)

        track_color = self._color_on if self._checked else self._color_off
        if not self.isEnabled():
            track_color = self._color_disabled
        painter.setBrush(QBrush(track_color))
        painter.setPen(QPen(track_color.darker(110), 1))
        painter.drawRoundedRect(track, switch_height // 2, switch_height // 2)

        thumb_diameter = switch_height - self._thumb_margin
        thumb_radius = thumb_diameter // 2
        min_x = switch_x + self._thumb_margin
        max_x = switch_x + self._switch_width - thumb_diameter - self._thumb_margin
        thumb_x = min_x + (max_x - min_x) * self._position
        thumb_y = switch_y + switch_height // 2

        shadow_rect = QRect(
            int(thumb_x) + self._shadow_offset,
            int(thumb_y) - thumb_radius + self._shadow_offset,
            thumb_diameter,
            thumb_diameter,
        )
        painter.setBrush(QBrush(QColor(0, 0, 0, 50)))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(shadow_rect)

        thumb_rect = QRect(
            int(thumb_x),
            int(thumb_y) - thumb_radius,
            thumb_diameter,
            thumb_diameter,
        )
        painter.setBrush(QBrush(self._thumb_color))
        border_color = QColor("#60a5fa") if self._hover and self.isEnabled() else QColor("#e2e8f0")
        painter.setPen(QPen(border_color, 1.4))
        painter.drawEllipse(thumb_rect)

        if self._text:
            painter.setPen(QPen(QColor("#e2e8f0")))
            painter.setFont(self._font)
            text_x = switch_x + self._switch_width + self._padding
            text_rect = QRect(text_x, 0, self.width() - text_x - self._tail_padding, self.height())
            painter.drawText(
                text_rect,
                Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                self._text,
            )

    def toggle(self) -> None:
        self.setChecked(not self._checked)

    def setChecked(self, checked: bool) -> None:
        if self._checked == checked:
            return
        self._checked = checked
        target = 1.0 if checked else 0.0
        self._animation.stop()
        self._animation.setStartValue(self._position)
        self._animation.setEndValue(target)
        self._animation.start()
        self.toggled.emit(checked)

    def isChecked(self) -> bool:
        return self._checked

    def setText(self, text: str) -> None:
        self._text = text
        self._update_size()
        self.update()

    def set_min_text_width(self, text_width: int) -> None:
        base = self._base_width()
        total = base if not self._text else base + self._padding + text_width + self._tail_padding
        if total > self._min_width:
            self._min_width = total
            self.setFixedWidth(self._min_width)
            self.updateGeometry()
        self.update()


class StyledDelegate(QStyledItemDelegate):
    def paint(self, painter, option, index):
        option = QStyleOptionViewItem(option)
        self.initStyleOption(option, index)
        option.state &= ~QStyle.StateFlag.State_HasFocus
        style = self._resolve_style(option)
        style.drawPrimitive(QStyle.PrimitiveElement.PE_PanelItemViewItem, option, painter, option.widget)

        if not (index.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            self._draw_locked_marker(painter, option, style)
            return

        indicator = QStyleOptionButton()
        indicator.state = QStyle.StateFlag.State_Enabled
        if self._check_state(index) == Qt.CheckState.Checked:
            indicator.state |= QStyle.StateFlag.State_On
        else:
            indicator.state |= QStyle.StateFlag.State_Off

        indicator.rect = self._indicator_rect(option, style)
        style.drawControl(
            QStyle.ControlElement.CE_CheckBox,
            indicator,
            painter,
            option.widget,
        )

        if indicator.state & QStyle.StateFlag.State_On:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            r = indicator.rect
            pen = QPen(QColor("#f8fafc"), 1.8, Qt.PenStyle.SolidLine,
                       Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            x1 = r.left() + int(r.width() * 0.22)
            y1 = r.top()  + int(r.height() * 0.50)
            x2 = r.left() + int(r.width() * 0.42)
            y2 = r.top()  + int(r.height() * 0.72)
            x3 = r.left() + int(r.width() * 0.78)
            y3 = r.top()  + int(r.height() * 0.28)
            painter.drawLine(QPoint(x1, y1), QPoint(x2, y2))
            painter.drawLine(QPoint(x2, y2), QPoint(x3, y3))
            painter.restore()

    def editorEvent(self, event, model, option, index):
        if not (index.flags() & Qt.ItemFlag.ItemIsUserCheckable):
            return False

        event_type = event.type()
        if event_type not in (QEvent.Type.MouseButtonRelease, QEvent.Type.KeyPress):
            return False

        if event_type == QEvent.Type.MouseButtonRelease:
            if event.button() != Qt.MouseButton.LeftButton:
                return False
            style = self._resolve_style(option)
            indicator_rect = self._indicator_rect(option, style)
            if not indicator_rect.contains(event.position().toPoint()):
                return False
        elif event.key() not in (Qt.Key.Key_Space, Qt.Key.Key_Select):
            return False

        checked = self._check_state(index) == Qt.CheckState.Checked
        new_state = Qt.CheckState.Unchecked if checked else Qt.CheckState.Checked
        return model.setData(index, new_state, Qt.ItemDataRole.CheckStateRole)

    @staticmethod
    def _check_state(index) -> Qt.CheckState:
        return Qt.CheckState(index.data(Qt.ItemDataRole.CheckStateRole) or 0)

    @staticmethod
    def _indicator_rect(option: QStyleOptionViewItem, style: QStyle) -> QRect:
        indicator = QStyleOptionButton()
        indicator.rect = option.rect
        width = style.pixelMetric(QStyle.PixelMetric.PM_IndicatorWidth, indicator, option.widget)
        height = style.pixelMetric(QStyle.PixelMetric.PM_IndicatorHeight, indicator, option.widget)
        x = option.rect.x() + (option.rect.width() - width) // 2
        y = option.rect.y() + (option.rect.height() - height) // 2
        return QRect(x, y, width, height)

    @classmethod
    def _draw_locked_marker(cls, painter, option: QStyleOptionViewItem, style: QStyle) -> None:
        rect = cls._indicator_rect(option, style)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(MISMATCH_COLOR), 2.0, Qt.PenStyle.SolidLine,
                   Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        margin = int(rect.width() * 0.28)
        y = rect.top() + rect.height() // 2
        painter.drawLine(QPoint(rect.left() + margin, y), QPoint(rect.right() - margin, y))
        painter.restore()

    @staticmethod
    def _resolve_style(option: QStyleOptionViewItem) -> QStyle:
        return option.widget.style() if option.widget else QApplication.style()


class FullTextDelegate(QStyledItemDelegate):
    def initStyleOption(self, option: QStyleOptionViewItem, index):
        super().initStyleOption(option, index)
        option.textElideMode = Qt.TextElideMode.ElideNone


class RegistrySentinel(QMainWindow):
    HEADERS = TABLE_HEADERS
    ENTRY_ROLE = Qt.ItemDataRole.UserRole + 1
    _BASE_BG_ROLE = Qt.ItemDataRole.UserRole + 2

    def __init__(self) -> None:
        super().__init__()
        script_source = sys.argv[0] if sys.argv and sys.argv[0] else __file__
        script_name = Path(script_source).name
        self.setWindowTitle(script_name or "Registry Sentinel")
        self.setMinimumSize(1180, 722)
        self.setFont(FONT_SEGOE)

        self._init_state()
        self._init_ui()

        self._load_last_session()
        geometry_restored = self._restore_window_geometry()
        if not geometry_restored:
            self._apply_pending_window_size(defer=True)
        self._load_default_list()

        if self._entries:
            QTimer.singleShot(0, self._start_scan)

    def _init_state(self) -> None:
        self._entries: list[RegistryEntry] = []
        self._check_item_lookup: dict[str, QTableWidgetItem] = {}
        self._list_path: Optional[Path] = None
        self._column_widths: dict[str, int] = {}
        self._pending_window_size: Optional[QSize] = None
        self._pending_window_pos: Optional[QPoint] = None
        self._state_toggles: dict[str, ToggleSwitch] = {}
        self._operation_visibility: dict[Operation, bool] = dict(DEFAULT_OPERATION_VISIBILITY)

        self._scan_worker: Optional[ScanWorker] = None
        self._apply_worker: Optional[ApplyWorker] = None
        self._live_workers: set[_WorkerBase] = set()

        self._scan_completed: bool = False
        self._apply_in_progress: bool = False
        self._apply_summary: str = ""

        self._find_matches: list[str] = []
        self._find_index: int = -1
        self._find_highlighted: list[QTableWidgetItem] = []
        self._find_highlight_brush = QBrush(QColor(59, 130, 246, 95))
        self._noncompliant_brush = QBrush(QColor(248, 113, 113, 15))
        self._mismatch_brush = QBrush(QColor(255, 32, 32, 52))
        self._visible_entry_ids: list[str] = []
        self._visible_entry_set: set[str] = set()
        self._last_filter_signature: Optional[tuple[Any, ...]] = None

        self._operation_started_at: dict[str, float] = {}
        self._sort_by_path_next: bool = True

        self._progress_reset_timer = QTimer(self)
        self._progress_reset_timer.setSingleShot(True)
        self._progress_reset_timer.timeout.connect(self._reset_progress)

        self._settings_save_timer = QTimer(self)
        self._settings_save_timer.setSingleShot(True)
        self._settings_save_timer.timeout.connect(self._save_last_session)

        self._search_debounce_timer = QTimer(self)
        self._search_debounce_timer.setSingleShot(True)
        self._search_debounce_timer.setInterval(150)
        self._search_debounce_timer.timeout.connect(self._apply_filters)

    def _init_ui(self) -> None:
        self._toolbar = self._build_toolbar()
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, self._toolbar)

        central = QWidget(self)
        self.setCentralWidget(central)

        layout = QVBoxLayout(central)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        layout.addLayout(self._build_filter_bar())
        layout.addWidget(self._build_table())

        self._footer_bar = self._build_status_bar()
        layout.addWidget(self._footer_bar)
        self._set_status("ready", "Ready")

        for attr, sequence, handler in (
            ("_shortcut_find", "Ctrl+F", self._focus_inline_find),
            ("_shortcut_find_next", "F3", self._find_next),
            ("_shortcut_find_prev", "Shift+F3", self._find_prev),
            ("_shortcut_goto_uid", "Ctrl+G", self._prompt_go_to_uid),
        ):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(handler)
            setattr(self, attr, shortcut)

        self._update_action_states()

    def _build_toolbar(self) -> QToolBar:
        toolbar = QToolBar("Main")
        toolbar.setIconSize(QSize(22, 22))
        toolbar.setMovable(False)
        toolbar.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        style = self.style()

        def make_action(
            icon: QStyle.StandardPixmap,
            text: str,
            handler: Callable[[], None],
            shortcut: QKeySequence | str | None = None,
        ) -> QAction:
            action = QAction(style.standardIcon(icon), text, self)
            if shortcut:
                action.setShortcut(shortcut if isinstance(shortcut, QKeySequence) else QKeySequence(shortcut))
            action.triggered.connect(handler)
            return action

        action_specs: list[ActionSpec] = [
            ActionSpec(QStyle.StandardPixmap.SP_FileDialogContentsView, "Load List", self._load_program_list, attr="_act_load_list", shortcut=QKeySequence(QKeySequence.StandardKey.Open), role="primary", tooltip="Load the default registry command list (Ctrl+O)"),
            ActionSpec(QStyle.StandardPixmap.SP_DialogOpenButton, "Browse List", self._choose_list_file, attr="_act_browse_list", role="primary", tooltip="Browse for a registry command list file"),
            ActionSpec(QStyle.StandardPixmap.SP_FileDialogInfoView, "View List", self._open_list_external, role="primary", tooltip="Open the current list file in the default editor"),
            ActionSpec(QStyle.StandardPixmap.SP_BrowserReload, "Run Scan", self._start_scan, attr="_act_run_scan", shortcut="F5", role="warning", tooltip="F5 · Scan all loaded entries against the current registry"),
            ActionSpec(QStyle.StandardPixmap.SP_DialogApplyButton, "Apply Selected", lambda: self._start_apply(selected_only=True), attr="_act_apply_selected", role="success", tooltip="Apply registry fixes to selected non-compliant entries"),
            ActionSpec(QStyle.StandardPixmap.SP_MediaPlay, "Apply All", lambda: self._start_apply(selected_only=False), attr="_act_apply_all", role="success", tooltip="Apply registry fixes to all visible non-compliant entries"),
            ActionSpec(QStyle.StandardPixmap.SP_DesktopIcon, "Reset View", self._reset_view),
            ActionSpec(QStyle.StandardPixmap.SP_FileDialogDetailedView, "View Log", self._view_log),
            ActionSpec(QStyle.StandardPixmap.SP_MessageBoxQuestion, "Help", lambda: self._info("Registry Sentinel", HELP_TEXT), shortcut=QKeySequence(QKeySequence.StandardKey.HelpContents)),
            ActionSpec(QStyle.StandardPixmap.SP_DialogSaveButton, "Export CSV Report", self._export_csv),
        ]

        SEPARATOR_AFTER_INDICES = {2, 3, 5}
        actions_with_roles: list[tuple[QAction, str]] = []
        for index, spec in enumerate(action_specs):
            action = make_action(spec.icon, spec.text, spec.handler, spec.shortcut)
            if spec.tooltip:
                action.setToolTip(spec.tooltip)
            if spec.attr:
                setattr(self, spec.attr, action)
            toolbar.addAction(action)
            actions_with_roles.append((action, spec.role))
            if index in SEPARATOR_AFTER_INDICES:
                toolbar.addSeparator()

        def apply_roles() -> None:
            for action, role in actions_with_roles:
                button = toolbar.widgetForAction(action)
                if not isinstance(button, QToolButton):
                    continue
                button.setProperty("colorRole", role)
                metrics = button.fontMetrics()
                text_width = metrics.horizontalAdvance(action.text())
                icon_width = button.iconSize().width() if button.iconSize().width() > 0 else 0
                spacing = button.style().pixelMetric(QStyle.PixelMetric.PM_ToolBarItemSpacing, None, button)
                if spacing < 0:
                    spacing = 10
                min_width = text_width + icon_width + spacing + TOOLBAR_BUTTON_PADDING
                base_width = TOOLBAR_BUTTON_WIDTH_PRIMARY if role in {"primary", "success"} else TOOLBAR_BUTTON_WIDTH_SECONDARY
                min_width = max(min_width, base_width)
                button.setMinimumWidth(int(min_width * 0.85))
                button.setMinimumHeight(38)
                button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
                button.setAutoRaise(False)
                button.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)
                style = button.style()
                style.unpolish(button)
                style.polish(button)
                button.update()

        QTimer.singleShot(0, apply_roles)

        return toolbar

    def _launch_worker(
        self,
        worker: _WorkerBase,
        *,
        finished: Callable[[Any], None],
        failed: Callable[[str], None],
    ) -> _WorkerBase:
        worker.progress_changed.connect(self._update_progress)
        worker.completed.connect(finished)
        worker.failed.connect(failed)
        self._live_workers.add(worker)
        worker.finished.connect(self._retire_worker)
        worker.start()
        return worker

    def _retire_worker(self) -> None:
        worker = self.sender()
        if not isinstance(worker, _WorkerBase):
            return
        self._live_workers.discard(worker)
        worker.deleteLater()

    @staticmethod
    def _is_running(worker: Optional[QThread]) -> bool:
        if worker is None:
            return False
        try:
            return worker.isRunning()
        except RuntimeError:
            return False

    def _update_action_states(self) -> None:
        in_progress = self._apply_in_progress or self._is_running(self._apply_worker)
        can_interact = self._scan_completed and bool(self._entries) and not in_progress
        noncompliant, selected = self._noncompliant_groups() if can_interact else ([], [])
        self._act_apply_all.setEnabled(can_interact and bool(noncompliant))
        self._act_apply_selected.setEnabled(can_interact and bool(selected))
        self._act_run_scan.setEnabled(bool(self._entries) and not in_progress)
        self._act_load_list.setEnabled(not in_progress)
        self._act_browse_list.setEnabled(not in_progress)

    @staticmethod
    def _create_inline_button(
        text: str,
        handler: Callable[[], None],
        *,
        object_name: str,
    ) -> QToolButton:
        button = QToolButton()
        button.setObjectName(object_name)
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        button.clicked.connect(handler)
        return button

    def _build_filter_bar(self) -> QHBoxLayout:
        bar = QHBoxLayout()
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(12)

        panel = QFrame()
        panel.setObjectName("inlineFindPanel")

        container = QHBoxLayout(panel)
        container.setContentsMargins(0, 0, 0, 0)
        container.setSpacing(12)

        icon_label = QLabel("🔍")
        icon_label.setObjectName("findIcon")
        container.addWidget(icon_label)

        self._inline_find_input = QLineEdit()
        self._inline_find_input.setObjectName("inlineFindInput")
        self._inline_find_input.setPlaceholderText("Find in registry entries (Ctrl+F)")
        self._inline_find_input.setClearButtonEnabled(False)
        self._inline_find_input.textChanged.connect(lambda _: self._search_debounce_timer.start())
        self._inline_find_input.returnPressed.connect(self._find_next)
        container.addWidget(self._inline_find_input)

        self._inline_find_count = QLabel("0/0")
        self._inline_find_count.setObjectName("inlineCount")
        self._inline_find_count.setVisible(False)
        container.addWidget(self._inline_find_count)

        for text, handler, obj_name in (
            ("Prev", self._find_prev, "inlineFindNavButton"),
            ("Next", self._find_next, "inlineFindNavButton"),
            ("Clear", self._clear_inline_find, "inlineFindClearButton"),
        ):
            container.addWidget(self._create_inline_button(text, handler, object_name=obj_name))

        bar.addWidget(panel)
        bar.addStretch(1)

        return bar

    def _build_table(self) -> QTableWidget:
        self.table = SentinelTable(0, len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        header = StyledHeader(self.table)
        self.table.setHorizontalHeader(header)
        self.table.setTextElideMode(Qt.TextElideMode.ElideNone)
        header.setStretchLastSection(False)
        header.setDefaultSectionSize(220)
        header.setMinimumSectionSize(60)
        for col in range(self.table.columnCount()):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)

        for col, text in ((UID_COLUMN, "UID"), (SELECT_COLUMN, "Select")):
            header_item = self.table.horizontalHeaderItem(col)
            if header_item:
                header_item.setText(text)
                header_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        full_text_delegate = FullTextDelegate(self.table)
        self.table.setItemDelegate(full_text_delegate)
        self.table.setItemDelegateForColumn(SELECT_COLUMN, StyledDelegate(self.table))

        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for bar, set_mode in ((self.table.verticalScrollBar, self.table.setVerticalScrollMode),
                                (self.table.horizontalScrollBar, self.table.setHorizontalScrollMode)):
            set_mode(QAbstractItemView.ScrollMode.ScrollPerPixel)
            bar().setSingleStep(12)
        self.table.itemChanged.connect(self._handle_item_changed)
        self.table.setSortingEnabled(True)
        header.setSectionsClickable(True)
        header.setSectionsMovable(False)
        header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        header.sectionClicked.connect(self._handle_header_clicked)
        header.sortIndicatorChanged.connect(self._handle_sort_changed)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(self._show_header_menu)
        header.sectionResized.connect(self._handle_section_resized)

        self.table.verticalHeader().setDefaultSectionSize(26)
        self.table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._show_table_menu)
        self._apply_default_column_visibility()
        return self.table

    def _create_state_toggle(self, state: str) -> ToggleSwitch:
        config = STATE_CONFIG[state]
        toggle = ToggleSwitch(
            config["label"].format(count=0),
            color_on=config["color"],
            color_off=TOGGLE_OFF_COLOR,
        )
        toggle.setChecked(config["default"])
        toggle.toggled.connect(lambda _: self._apply_filters())
        self._state_toggles[state] = toggle
        return toggle

    def _build_status_bar(self) -> QWidget:
        footer = QFrame()
        footer.setObjectName("footerBar")

        layout = QHBoxLayout(footer)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(16)

        status_container = QFrame()
        status_container.setObjectName("statusContainer")
        status_layout = QHBoxLayout(status_container)
        status_layout.setContentsMargins(8, 2, 8, 2)
        status_layout.setSpacing(10)

        self._status_icon = QLabel("●")
        self._status_icon.setStyleSheet(STATUS_STYLE_BASE + "color: #38bdf8;")
        self._status_glow = QGraphicsDropShadowEffect(self)
        self._status_glow.setBlurRadius(0)
        self._status_glow.setOffset(0, 0)
        self._status_icon.setGraphicsEffect(self._status_glow)

        self._status_animation = QPropertyAnimation(self._status_glow, b"blurRadius", self)
        self._status_animation.setDuration(1200)
        self._status_animation.setKeyValueAt(0.0, 6)
        self._status_animation.setKeyValueAt(0.5, 26)
        self._status_animation.setKeyValueAt(1.0, 6)
        self._status_animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        self._status_animation.setLoopCount(-1)

        message_widget = QWidget()
        message_widget.setObjectName("statusMessage")
        message_widget.setFixedWidth(STATUS_MESSAGE_WIDTH)
        message_widget.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        message_layout = QVBoxLayout(message_widget)
        message_layout.setContentsMargins(0, 2, 0, 2)
        message_layout.setSpacing(2)

        self._status_label = QLabel("Ready")
        self._status_label.setObjectName("statusText")
        self._status_label.ensurePolished()
        self._status_label.setWordWrap(False)
        self._status_label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._status_label.setFixedWidth(STATUS_MESSAGE_WIDTH)
        self._status_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)

        self._status_timer = QLabel()
        self._status_timer.setObjectName("timerText")
        self._status_timer.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._status_timer.setFixedWidth(STATUS_MESSAGE_WIDTH)

        message_layout.addWidget(self._status_label)
        message_layout.addWidget(self._status_timer)

        status_layout.addWidget(self._status_icon)
        status_layout.addWidget(message_widget)

        self._progress = QProgressBar()
        self._progress.setRange(0, 100)
        self._progress.setValue(0)
        self._progress.setFixedHeight(22)
        self._progress.setTextVisible(False)
        self._progress.setFormat("%v / %m")
        self._progress.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._progress.setMinimumWidth(180)
        self._progress.setObjectName("footerProgress")
        self._progress_animation = QPropertyAnimation(self._progress, b"value", self)
        self._progress_animation.setDuration(250)
        self._progress_animation.setEasingCurve(QEasingCurve.Type.InOutCubic)

        toggle_container = QFrame()
        toggle_container.setObjectName("toggleContainer")
        toggle_layout = QHBoxLayout(toggle_container)
        toggle_layout.setContentsMargins(0, 0, 0, 0)
        toggle_layout.setSpacing(0)
        toggle_gap = 16
        toggle_container.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        self._footer_entries_label = QLabel("Entries: 0")
        self._footer_entries_label.setObjectName("footerEntries")
        self._footer_entries_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)

        toggle_layout.addWidget(self._footer_entries_label)
        states = list(TOGGLE_STATES)
        if states:
            toggle_layout.addSpacing(toggle_gap)
        for index, state in enumerate(states):
            toggle_layout.addWidget(self._create_state_toggle(state))
            if index < len(states) - 1:
                toggle_layout.addSpacing(toggle_gap)

        layout.addWidget(status_container)
        layout.addWidget(self._progress, stretch=2)
        layout.addWidget(toggle_container, alignment=Qt.AlignmentFlag.AlignRight)

        self._reserve_footer_toggle_widths()

        return footer

    def _set_status(
        self,
        state: str,
        message: str,
        duration_text: str = "",
        tooltip: str = "",
    ) -> None:
        style, glow_color, glyph = STATUS_STYLES.get(state, STATUS_STYLES["ready"])
        self._status_icon.setText(glyph)
        self._status_icon.setStyleSheet(f"{STATUS_STYLE_BASE}{style}")
        self._status_animation.stop()
        if glow_color:
            self._status_glow.setColor(QColor(glow_color))
            self._status_animation.start()
        else:
            self._status_glow.setBlurRadius(0)
            self._status_glow.setColor(QColor(0, 0, 0, 0))
        elided = self._status_label.fontMetrics().elidedText(
            message, Qt.TextElideMode.ElideRight, STATUS_MESSAGE_WIDTH
        )
        if not tooltip and elided != message:
            tooltip = message
        self._status_label.setText(elided)
        self._status_label.setToolTip(tooltip)
        self._status_timer.setText(duration_text)

    def _begin_status(self, key: str, message: str, *, total: Optional[int] = None) -> None:
        self._operation_started_at[key] = time.perf_counter()
        self._set_status("working", message)
        self._progress_reset_timer.stop()
        if total and total > 0:
            self._progress.setTextVisible(True)
            self._progress.setRange(0, total)
            self._animate_progress(0)
        else:
            self._progress.setTextVisible(False)
            self._progress.setRange(0, 0)
            self._progress.setValue(0)

    def _end_status(
        self,
        key: str,
        message: str,
        *,
        extra: str = "",
        state: str = "success",
    ) -> None:
        duration_text = self._format_duration_text(key, extra=extra)
        self._set_status(state, message, duration_text)
        (self._complete_progress if state == "success" else self._reset_progress)()

    def _animate_progress(self, value: int) -> None:
        if self._progress.minimum() == 0 and self._progress.maximum() == 0:
            self._progress.setValue(0)
            return
        self._progress_animation.stop()
        self._progress_animation.setStartValue(self._progress.value())
        self._progress_animation.setEndValue(int(value))
        self._progress_animation.start()

    def _reset_progress(self) -> None:
        self._progress_reset_timer.stop()
        self._progress.setRange(0, 100)
        self._progress_animation.stop()
        self._progress.setValue(0)
        self._progress.setTextVisible(False)

    def _schedule_progress_reset(self) -> None:
        self._progress_reset_timer.start(PROGRESS_RESET_DELAY_MS)

    def _format_duration_text(self, key: str, *, extra: str = "") -> str:
        start = self._operation_started_at.pop(key, None)
        parts: list[str] = []
        if extra:
            parts.append(extra)
        if start is not None:
            parts.append(f"{time.perf_counter() - start:.2f}s")
        return " · ".join(parts)

    def _complete_progress(self) -> None:
        if self._progress.maximum() > self._progress.minimum():
            self._animate_progress(self._progress.maximum())
            self._schedule_progress_reset()
        else:
            self._reset_progress()

    def _reserve_footer_toggle_widths(self) -> None:
        self._footer_entries_label.ensurePolished()
        fm = self._footer_entries_label.fontMetrics()
        widest = (
            "Entries: 88888 (hidden 88888) · "
            + STATE_CONFIG[MISMATCH_STATE]["label"].format(count=88888)
        )
        self._footer_entries_label.setFixedWidth(fm.horizontalAdvance(widest))

        fm = QFontMetrics(FONT_SEGOE_BOLD_8)
        samples = [STATE_CONFIG[state]["label"].format(count=88888) for state in TOGGLE_STATES]
        toggle_width = max(fm.horizontalAdvance(s) for s in samples) + fm.horizontalAdvance("  ")
        for toggle in self._state_toggles.values():
            toggle.set_min_text_width(toggle_width)

    def _reset_state_toggles(self) -> None:
        changed = False
        for state in TOGGLE_STATES:
            config = STATE_CONFIG[state]
            toggle = self._state_toggles[state]
            if toggle.isChecked() != config["default"]:
                toggle.blockSignals(True)
                toggle.setChecked(config["default"])
                toggle.blockSignals(False)
                changed = True
        if changed:
            self._apply_filters()

    def _update_toggle_labels(self, totals_by_category: dict[str, int]) -> None:
        for state in TOGGLE_STATES:
            config = STATE_CONFIG[state]
            self._state_toggles[state].setText(config["label"].format(count=totals_by_category.get(state, 0)))


    def _settings_path(self) -> Path:
        return _program_dir() / "registry_sentinel.ini"

    def _default_list_dirs(self) -> tuple[Path, Path]:
        return (_program_dir(), Path.cwd())

    def _candidate_lists_from(self, *directories: Path) -> Iterable[Path]:
        return (
            directory / f"{DEFAULT_LIST_BASENAME}{ext}"
            for directory in directories
            for ext in sorted(SUPPORTED_LIST_EXTENSIONS)
        )

    def _load_first_available_list(self, candidates: Iterable[Path]) -> bool:
        for candidate in candidates:
            if candidate.exists():
                self._load_entries(candidate)
                return True
        return False

    _SETTINGS_SECTION = "Session"

    def _load_last_session(self) -> None:
        path = self._settings_path()
        cfg = configparser.ConfigParser(interpolation=None)
        if path.exists():
            try:
                cfg.read(path, encoding="utf-8")
            except (configparser.Error, OSError) as exc:
                logger.warning("Ignoring unreadable settings file %s: %s", path, exc)
        section = self._SETTINGS_SECTION
        size_raw = cfg.get(section, "window_size", fallback=None)
        if size_raw:
            self._pending_window_size = self._parse_window_size(size_raw)
        self._pending_window_size = self._pending_window_size or QSize(*DEFAULT_WINDOW_SIZE)
        pos_raw = cfg.get(section, "window_pos", fallback=None)
        if pos_raw:
            self._pending_window_pos = self._parse_window_pos(pos_raw)
        columns_raw = cfg.get(section, "column_widths", fallback=None)
        parsed_columns: dict[str, int] = (
            self._parse_column_widths(columns_raw) if columns_raw else {}
        )
        self._column_widths = {**DEFAULT_COLUMN_WIDTHS, **parsed_columns}
        self._apply_saved_column_widths()
        hidden_raw = cfg.get(section, "hidden_columns", fallback=None)
        if hidden_raw is not None:
            _critical_cols = {self.HEADERS[UID_COLUMN], self.HEADERS[SELECT_COLUMN]}
            saved_hidden = {name.strip() for name in hidden_raw.split(",") if name.strip()} - _critical_cols
            for idx, name in enumerate(self.HEADERS):
                self.table.setColumnHidden(idx, name in saved_hidden)
        operations_raw = cfg.get(section, "operation_filter", fallback=None)
        if operations_raw is not None:
            self._operation_visibility = self._parse_operation_filter(operations_raw)
        self._update_type_header()
        last_file = cfg.get(section, "last_file", fallback=None)
        if last_file:
            last_path = Path(last_file)
            if last_path.exists():
                self._load_entries(last_path)

    def _load_default_list(self, *, force: bool = False) -> bool:
        if self._entries and not force:
            return True

        candidates = self._candidate_lists_from(*self._default_list_dirs())
        return self._load_first_available_list(candidates)

    def _save_last_session(self) -> None:
        path = self._settings_path()
        cfg = configparser.ConfigParser(interpolation=None)
        section = self._SETTINGS_SECTION
        cfg.add_section(section)
        if self._list_path:
            cfg.set(section, "last_file", str(self._list_path))
        size = self.size()
        if size.isValid():
            cfg.set(section, "window_size", f"{size.width()}x{size.height()}")
        pos = self.pos()
        cfg.set(section, "window_pos", f"{pos.x()},{pos.y()}")
        if self._column_widths:
            serialized = self._serialize_column_widths(self._column_widths)
            if serialized:
                cfg.set(section, "column_widths", serialized)
        hidden_cols = ",".join(
            self.HEADERS[i] for i in range(self.table.columnCount()) if self.table.isColumnHidden(i)
        )
        cfg.set(section, "hidden_columns", hidden_cols)
        cfg.set(section, "operation_filter", self._serialize_operation_filter())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("w", encoding="utf-8") as fh:
                cfg.write(fh)
        except OSError as exc:
            logger.warning("Unable to save settings to %s: %s", path, exc)

    def _handle_section_resized(self, logical_index: int, _old_size: int, _new_size: int) -> None:
        if 0 <= logical_index < len(self.HEADERS):
            header_name = self.HEADERS[logical_index]
            width = self.table.columnWidth(logical_index)
            if width > 0:
                self._column_widths[header_name] = width
        self._settings_save_timer.start(SETTINGS_SAVE_DELAY_MS)

    def _apply_saved_column_widths(self) -> None:
        if not self._column_widths:
            return
        header = self.table.horizontalHeader()
        minimum = max(header.minimumSectionSize(), 40)
        for index, header_name in enumerate(self.HEADERS):
            width = self._column_widths.get(header_name)
            if width and width > 0:
                self.table.setColumnWidth(index, max(width, minimum))


    def _restore_window_geometry(self) -> bool:
        if self._pending_window_pos is None:
            return False
        if self._pending_window_size is not None:
            self.resize(self._pending_window_size)
            self._pending_window_size = None
        pos = self._pending_window_pos
        self._pending_window_pos = None
        screen = QApplication.screenAt(pos) or QApplication.primaryScreen()
        if screen is not None:
            available = screen.availableGeometry()
            win_size = self.size()
            x = max(available.left(), min(pos.x(), available.right() - win_size.width()))
            y = max(available.top(), min(pos.y(), available.bottom() - win_size.height()))
            pos = QPoint(x, y)
        self.move(pos)
        return True

    def _apply_pending_window_size(self, *, defer: bool = False) -> None:
        if not self._pending_window_size:
            return
        if defer:
            QTimer.singleShot(0, self._apply_pending_window_size)
            return
        self.resize(self._pending_window_size)
        self._center_on_primary(self._pending_window_size)
        self._pending_window_size = None

    def _center_on_primary(self, size: QSize) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            self.move(0, 0)
            return
        available = screen.availableGeometry()
        x = available.x() + max((available.width() - size.width()) // 2, 0)
        y = available.y() + max((available.height() - size.height()) // 2, 0)
        self.move(x, y)

    def _serialize_operation_filter(self) -> str:
        return ",".join(
            operation.value
            for operation in DEFAULT_OPERATION_VISIBILITY
            if self._operation_visibility.get(operation, True)
        )

    @staticmethod
    def _parse_operation_filter(raw: str) -> dict[Operation, bool]:
        saved = {name.strip().upper() for name in raw.split(",") if name.strip()}
        parsed = {operation: operation.value in saved for operation in DEFAULT_OPERATION_VISIBILITY}
        return parsed if any(parsed.values()) else dict(DEFAULT_OPERATION_VISIBILITY)

    @staticmethod
    def _serialize_column_widths(widths: dict[str, int]) -> str:
        parts = [f"{name}:{width}" for name, width in widths.items() if width > 0]
        return ",".join(parts)

    @staticmethod
    def _parse_column_widths(raw: str) -> dict[str, int]:
        result: dict[str, int] = {}
        if not raw:
            return result
        for chunk in raw.split(","):
            if not chunk or ":" not in chunk:
                continue
            name, value = chunk.split(":", 1)
            name = name.strip()
            try:
                width = int(value)
            except ValueError:
                continue
            if width > 0:
                result[name] = width
        return result

    @staticmethod
    def _parse_int_pair(raw: str, sep: str) -> Optional[tuple[int, int]]:
        if not raw or sep not in raw:
            return None
        a_str, b_str = raw.split(sep, 1)
        try:
            return int(a_str.strip()), int(b_str.strip())
        except ValueError:
            return None

    @classmethod
    def _parse_window_size(cls, raw: str) -> Optional[QSize]:
        pair = cls._parse_int_pair(raw, "x")
        return QSize(*pair) if pair and pair[0] > 0 and pair[1] > 0 else None

    @classmethod
    def _parse_window_pos(cls, raw: str) -> Optional[QPoint]:
        pair = cls._parse_int_pair(raw, ",")
        return QPoint(*pair) if pair else None


    def _load_entries(self, file_path: Path) -> None:
        if file_path.suffix.lower() not in SUPPORTED_LIST_EXTENSIONS:
            self._warn(
                "Unsupported file",
                f"List files must use one of the following extensions: {SUPPORTED_LIST_DESC}",
            )
            return
        parser = RegistryCommandParser()
        try:
            result = parser.parse_file(file_path)
        except (OSError, UnicodeDecodeError) as exc:
            self._critical("Load Error", f"Failed to load list:\n{exc}")
            return

        if self._cancel_worker(self._scan_worker):
            self._reset_progress()
        self._scan_worker = None

        self._sort_by_path_next = True
        self._entries = result.entries
        self._list_path = file_path

        self._scan_completed = False
        self._update_action_states()

        self._populate_table()
        status_msg = f"Loaded {len(result.entries)} entries from {file_path.name}"
        if result.skipped_lines:
            status_msg += f" ({len(result.skipped_lines)} lines skipped)"
        self._set_status("ready", status_msg, tooltip=self._skipped_tooltip(result.skipped_lines))
        self._settings_save_timer.start(SETTINGS_SAVE_DELAY_MS)

    @staticmethod
    def _skipped_tooltip(skipped: Sequence[tuple[int, str]]) -> str:
        if not skipped:
            return ""
        shown = skipped[:MAX_SKIPPED_DISPLAY]
        lines = [f"Line {number}: {text}" for number, text in shown]
        if len(skipped) > len(shown):
            lines.append(f"… and {len(skipped) - len(shown)} more")
        return "Lines that produced no entries:\n" + "\n".join(lines)

    @staticmethod
    def _make_table_item(
        text: str,
        *,
        sort_value: Optional[Any] = None,
        sort_tiebreak: Sequence[Any] = (),
        pinned: bool = False,
    ) -> SentinelItem:
        item = SentinelItem(text, sort_value=sort_value, sort_tiebreak=sort_tiebreak, pinned=pinned)
        item.setFlags(STATIC_ITEM_FLAGS)
        return item

    def _create_row_items(self, row: int, entry: RegistryEntry) -> None:
        keys = _sort_keys(entry)
        uid_item = self._make_table_item(
            str(entry.source_line), sort_value=entry.source_line, sort_tiebreak=keys.then_by_path
        )
        uid_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

        check_item = QTableWidgetItem()
        check_item.setFlags(CHECKABLE_ITEM_FLAGS)
        check_item.setCheckState(Qt.CheckState.Checked if entry.selected else Qt.CheckState.Unchecked)
        check_item.setData(self.ENTRY_ROLE, entry)
        self._check_item_lookup[entry.unique_id] = check_item
        self.table.setItem(row, UID_COLUMN, uid_item)
        self.table.setItem(row, SELECT_COLUMN, check_item)

        cell_specs = [
            (PATH_COLUMN, entry.full_path, keys.path, keys.then_by_value),
            (VALUE_COLUMN, entry.display_name, keys.value, keys.then_by_path),
            (
                TYPE_COLUMN,
                entry.value_type.value,
                (entry.value_type.value, entry.operation.value),
                keys.then_by_path,
            ),
            (
                EXPECTED_COLUMN,
                entry.expected_display,
                entry.expected_display.casefold(),
                keys.then_by_path,
            ),
        ]
        for column, text, sort_value, tiebreak in cell_specs:
            self.table.setItem(
                row, column, self._make_table_item(text, sort_value=sort_value, sort_tiebreak=tiebreak)
            )

        if entry.is_reset:
            for column in (PATH_COLUMN, TYPE_COLUMN, EXPECTED_COLUMN):
                cell = self.table.item(row, column)
                if cell is not None:
                    cell.setToolTip(RESET_TOOLTIP)

        if entry.is_default_value or entry.has_literal_default_name:
            value_cell = self.table.item(row, VALUE_COLUMN)
            if value_cell is not None:
                value_cell.setToolTip(
                    DEFAULT_VALUE_TOOLTIP if entry.is_default_value else LITERAL_DEFAULT_TOOLTIP
                )

        for column in (ACTUAL_COLUMN, DETAILS_COLUMN):
            self.table.setItem(row, column, self._make_table_item("", sort_tiebreak=keys.then_by_path))
        self._apply_scan_state(row, entry)

    def _apply_scan_state(self, row: int, entry: RegistryEntry) -> None:
        state_key = self._compliance_key(entry)
        is_mismatch = state_key == MISMATCH_STATE

        actual_item = self.table.item(row, ACTUAL_COLUMN)
        if isinstance(actual_item, SentinelItem):
            actual_item.setText(entry.actual_display)
            actual_item.set_sort_value(entry.actual_display.casefold())

        detail_item = self.table.item(row, DETAILS_COLUMN)
        if isinstance(detail_item, SentinelItem):
            detail_item.setText(entry.detail_text)
            detail_item.set_sort_value((COMPLIANCE_ORDER[state_key], entry.detail_text.casefold()))
            detail_item.setForeground(QColor(STATE_CONFIG[state_key]["color"]))
            if is_mismatch:
                font = self.table.font()
                font.setBold(True)
                detail_item.setFont(font)
                detail_item.setToolTip(entry.detail_text)
            else:
                detail_item.setData(Qt.ItemDataRole.FontRole, None)
                detail_item.setToolTip("")

        if is_mismatch:
            row_bg = self._mismatch_brush
        elif state_key == "noncompliant":
            row_bg = self._noncompliant_brush
        else:
            row_bg = QBrush()

        applied = self.table.item(row, UID_COLUMN)
        current_bg = applied.data(self._BASE_BG_ROLE) if applied is not None else None
        if not isinstance(current_bg, QBrush):
            current_bg = QBrush()
        if current_bg != row_bg:
            for col in range(self.table.columnCount()):
                cell = self.table.item(row, col)
                if cell is None:
                    continue
                cell.setData(self._BASE_BG_ROLE, row_bg)
                cell.setBackground(row_bg)
                if isinstance(cell, SentinelItem):
                    cell.set_pinned(is_mismatch)

        self._set_row_locked(row, entry, locked=is_mismatch)

    def _set_row_locked(self, row: int, entry: RegistryEntry, *, locked: bool) -> None:
        check_item = self.table.item(row, SELECT_COLUMN)
        if check_item is None:
            return

        check_item.setFlags(STATIC_ITEM_FLAGS if locked else CHECKABLE_ITEM_FLAGS)

        if locked:
            entry.selected = False
            if check_item.checkState() != Qt.CheckState.Unchecked:
                check_item.setCheckState(Qt.CheckState.Unchecked)
            if entry.syntax_error:
                tooltip = SYNTAX_ERROR_TOOLTIP
            elif entry.conflict:
                tooltip = CONFLICT_TOOLTIP
            else:
                tooltip = TYPE_MISMATCH_TOOLTIP
            check_item.setToolTip(tooltip)
        else:
            check_item.setToolTip("")

    def _sort_table(self, column: int, order: Qt.SortOrder) -> None:
        header = self.table.horizontalHeader()
        header.setSortIndicator(column, order)
        self.table.sortItems(column, order)

    def _populate_table(self) -> None:
        sorting = self.table.isSortingEnabled()
        try:
            self.table.setUpdatesEnabled(False)
            self.table.blockSignals(True)
            self.table.setSortingEnabled(False)
            self._find_highlighted.clear()
            self.table.clearContents()
            self.table.setRowCount(len(self._entries))
            self._check_item_lookup = {}

            for row, entry in enumerate(self._entries):
                self._create_row_items(row, entry)
        finally:
            self.table.setSortingEnabled(sorting)
            if self._sort_by_path_next and self.table.rowCount():
                self._sort_table(PATH_COLUMN, Qt.SortOrder.AscendingOrder)
                self._sort_by_path_next = False
            self.table.blockSignals(False)
            self.table.setUpdatesEnabled(True)
            self._refresh_filters()

    def _search_query(self) -> str:
        return self._inline_find_input.text().strip()

    def _entry_for_row(self, row: int) -> Optional[RegistryEntry]:
        item = self.table.item(row, SELECT_COLUMN)
        if not item:
            return None
        entry = item.data(self.ENTRY_ROLE)
        return entry if isinstance(entry, RegistryEntry) else None

    def _row_entries(self) -> Iterable[tuple[int, Optional[RegistryEntry]]]:
        return (
            (row, self._entry_for_row(row)) for row in range(self.table.rowCount())
        )

    def _noncompliant_groups(self) -> tuple[list[RegistryEntry], list[RegistryEntry]]:
        noncompliant: list[RegistryEntry] = []
        selected: list[RegistryEntry] = []
        for e in self._entries:
            if e.compliant is not False or e.list_error or e.access_denied:
                continue
            if e.selected:
                selected.append(e)
            if e.unique_id in self._visible_entry_set:
                noncompliant.append(e)
        return noncompliant, selected

    @property
    def _list_error_count(self) -> int:
        return sum(1 for entry in self._entries if entry.list_error)

    def _compliance_key(self, entry: RegistryEntry) -> str:
        if entry.list_error:
            return MISMATCH_STATE
        return COMPLIANCE_TO_STATE.get(entry.compliant, "pending")

    def _state_visibility(self) -> dict[str, bool]:
        return {state: self._state_toggles[state].isChecked() for state in TOGGLE_STATES}

    def _apply_filters(self) -> None:
        query = self._search_query()
        normalized_query = query.casefold()
        state_visibility = self._state_visibility()
        operation_visibility = dict(self._operation_visibility)
        signature = (
            normalized_query,
            tuple(sorted(state_visibility.items())),
            tuple(sorted((operation.value, visible) for operation, visible in operation_visibility.items())),
        )

        if self._last_filter_signature != signature:
            self._last_filter_signature = signature
            totals_by_category: dict[str, int] = dict.fromkeys(STATE_CONFIG, 0)
            visible_ids: list[str] = []
            entry_count = 0

            for row, entry in self._row_entries():
                if entry is None:
                    self.table.setRowHidden(row, False)
                    continue

                entry_count += 1
                state_key = self._compliance_key(entry)
                matches_query = not normalized_query or normalized_query in entry.search_blob
                matches_operation = entry.list_error or operation_visibility.get(entry.operation, True)
                matches_filters = matches_query and matches_operation
                if matches_filters and not entry.access_denied:
                    totals_by_category[state_key] += 1

                if entry.list_error:
                    should_show = matches_filters
                else:
                    should_show = (
                        matches_filters
                        and state_visibility.get(state_key, True)
                        and not entry.access_denied
                    )
                self.table.setRowHidden(row, not should_show)

                if should_show:
                    visible_ids.append(entry.unique_id)

            self._visible_entry_ids = visible_ids
            self._visible_entry_set = set(visible_ids)
            hidden_count = entry_count - len(visible_ids)
            extra = f" (hidden {hidden_count})" if hidden_count else ""
            list_errors = totals_by_category[MISMATCH_STATE]
            if list_errors:
                extra += (
                    f' · <span style="color:{MISMATCH_COLOR}">'
                    f'{STATE_CONFIG[MISMATCH_STATE]["label"].format(count=list_errors)}</span>'
                )
            self._footer_entries_label.setText(f"Entries: {len(visible_ids)}{extra}")
            self._update_toggle_labels(totals_by_category)
            self._update_action_states()

        self._update_find_matches(select_current=True)

    def _refresh_filters(self) -> None:
        self._last_filter_signature = None
        self._apply_filters()

    def _handle_sort_changed(self, _index: int, _order: Qt.SortOrder) -> None:
        if self.table.rowCount():
            QTimer.singleShot(0, self._refresh_filters)

    def _handle_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != SELECT_COLUMN:
            return
        entry = item.data(self.ENTRY_ROLE)
        if not isinstance(entry, RegistryEntry):
            return
        selected = item.checkState() == Qt.CheckState.Checked
        if selected == entry.selected:
            return
        entry.selected = selected
        self._update_action_states()

    def _handle_header_clicked(self, index: int) -> None:
        if index == SELECT_COLUMN:
            self.table.horizontalHeader().setSortIndicator(-1, Qt.SortOrder.AscendingOrder)

    def _show_header_menu(self, pos: QPoint) -> None:
        header = self.table.horizontalHeader()
        logical_index = header.logicalIndexAt(pos)
        menu = QMenu(header)
        menu.setToolTipsVisible(True)

        hide_action = None
        if logical_index >= 0:
            title = self.HEADERS[logical_index] if logical_index < len(self.HEADERS) else "Column"
            hide_action = menu.addAction(f"Hide {title}")
            if self.table.isColumnHidden(logical_index) or logical_index in (UID_COLUMN, SELECT_COLUMN):
                hide_action.setEnabled(False)

        show_all_action = menu.addAction("Show All Columns")

        menu.addSeparator()
        operation_actions = self._add_operation_filter_actions(menu)

        chosen = menu.exec(header.mapToGlobal(pos))
        if chosen is None:
            return
        if chosen in operation_actions:
            self._set_operation_visible(operation_actions[chosen], chosen.isChecked())
        elif chosen == hide_action and logical_index >= 0:
            self.table.setColumnHidden(logical_index, True)
            self._settings_save_timer.start(SETTINGS_SAVE_DELAY_MS)
        elif chosen == show_all_action:
            self._show_all_columns()
            self._settings_save_timer.start(SETTINGS_SAVE_DELAY_MS)

    def _add_operation_filter_actions(self, menu: QMenu) -> dict[QAction, Operation]:
        actions: dict[QAction, Operation] = {}
        visible_count = sum(1 for visible in self._operation_visibility.values() if visible)
        for operation, label in OPERATION_FILTERS:
            action = menu.addAction(label)
            action.setCheckable(True)
            checked = self._operation_visibility.get(operation, True)
            action.setChecked(checked)
            if checked and visible_count <= 1:
                action.setEnabled(False)
                action.setToolTip(LAST_OPERATION_TOOLTIP)
            actions[action] = operation
        return actions

    def _set_operation_visible(self, operation: Operation, visible: bool) -> None:
        if self._operation_visibility.get(operation, True) == visible:
            return
        if not visible and not any(
            state for other, state in self._operation_visibility.items() if other is not operation
        ):
            return
        self._operation_visibility[operation] = visible
        self._update_type_header()
        self._refresh_filters()
        self._settings_save_timer.start(SETTINGS_SAVE_DELAY_MS)

    def _show_only_operations(self, operations: Iterable[Operation]) -> None:
        wanted = {operation: operation in set(operations) for operation in DEFAULT_OPERATION_VISIBILITY}
        if not any(wanted.values()) or wanted == self._operation_visibility:
            return
        self._operation_visibility = wanted
        self._update_type_header()

    def _update_type_header(self) -> None:
        header_item = self.table.horizontalHeaderItem(TYPE_COLUMN)
        if header_item is None:
            return
        hidden = [operation for operation, visible in self._operation_visibility.items() if not visible]
        label = self.HEADERS[TYPE_COLUMN]
        tooltip = TYPE_HEADER_TOOLTIP
        if hidden:
            shown = next(
                (operation for operation, visible in self._operation_visibility.items() if visible),
                None,
            )
            if shown is not None:
                label = OPERATION_ONLY_LABELS.get(shown, label)
                tooltip = f"Filtered to “reg {shown.value.lower()}” entries: {TYPE_HEADER_TOOLTIP.lower()}"
        header_item.setText(label)
        header_item.setToolTip(tooltip)

    def _show_all_columns(self) -> None:
        for col in range(self.table.columnCount()):
            self.table.setColumnHidden(col, False)

    def _apply_default_column_visibility(self) -> None:
        self.table.setColumnHidden(TYPE_COLUMN, True)

    def _clear_find_highlights(self) -> None:
        for item in self._find_highlighted:
            base = item.data(self._BASE_BG_ROLE)
            item.setBackground(base if isinstance(base, QBrush) else QBrush())
        self._find_highlighted.clear()

    def _update_find_matches(self, *, select_current: bool) -> None:
        previous_entry: Optional[str] = None
        if 0 <= self._find_index < len(self._find_matches):
            previous_entry = self._find_matches[self._find_index]

        self._clear_find_highlights()
        self._find_matches = list(self._visible_entry_ids) if self._search_query() else []

        if not self._find_matches:
            self._find_index = -1
            self._update_inline_find_count()
            return

        self._find_index = (
            self._find_matches.index(previous_entry) if previous_entry in self._find_matches else 0
        )
        self._select_find_match(set_current=select_current)

    def _advance_find(self, step: int) -> None:
        if not self._find_matches:
            self._focus_inline_find()
            return
        total = len(self._find_matches)
        self._find_index = (self._find_index + step) % total
        self._select_find_match()

    _find_next = partialmethod(_advance_find, 1)
    _find_prev = partialmethod(_advance_find, -1)

    def _select_find_match(self, *, set_current: bool = True) -> None:
        if self._find_matches and self._find_index >= 0:
            self._highlight_find_row(self._find_matches[self._find_index], set_current=set_current)
        self._update_inline_find_count()

    def _highlight_find_row(self, entry_id: str, *, set_current: bool) -> bool:
        check_item = self._check_item_lookup.get(entry_id)
        if check_item is None:
            return False

        row = check_item.row()
        self._clear_find_highlights()

        focus_col = PATH_COLUMN if self.table.columnCount() > PATH_COLUMN else 0
        item, focus_col = self.table.first_item_in_row(row, focus_col)
        if item is None:
            return False

        for col in range(self.table.columnCount()):
            cell = self.table.item(row, col)
            if cell is not None:
                cell.setBackground(self._find_highlight_brush)
                self._find_highlighted.append(cell)

        if set_current:
            self.table.setCurrentCell(
                row,
                focus_col,
                QItemSelectionModel.SelectionFlag.ClearAndSelect,
            )
            self.table.scrollToItem(item, QAbstractItemView.ScrollHint.PositionAtCenter)
        return True

    def _update_inline_find_count(self) -> None:
        total = len(self._find_matches)
        current = self._find_index + 1 if self._find_index >= 0 else 0
        has_query = bool(self._search_query())
        self._inline_find_count.setVisible(has_query)
        self._inline_find_count.setText(f"{current}/{total}" if total else "0/0")
        no_match = has_query and total == 0
        self._inline_find_input.setStyleSheet(
            "QLineEdit#inlineFindInput { border-color: #f87171; }" if no_match else ""
        )

    def _focus_inline_find(self, *, select_all: bool = True) -> None:
        self._inline_find_input.setFocus(Qt.FocusReason.ShortcutFocusReason)
        if select_all:
            self._inline_find_input.selectAll()

    def _clear_inline_find(self) -> None:
        self._inline_find_input.clear()
        self._focus_inline_find(select_all=False)

    def _go_to_uid(self, source_line: int) -> bool:
        target_uid = next(
            (e.unique_id for e in self._entries if e.source_line == source_line),
            None,
        )
        if target_uid is None:
            return False
        check_item = self._check_item_lookup.get(target_uid)
        if check_item is None:
            return False
        entry = check_item.data(self.ENTRY_ROLE)
        if not isinstance(entry, RegistryEntry):
            return False
        row = check_item.row()

        if self.table.isRowHidden(row):
            self._inline_find_input.blockSignals(True)
            self._inline_find_input.clear()
            self._inline_find_input.blockSignals(False)
            for toggle in self._state_toggles.values():
                toggle.blockSignals(True)
                toggle.setChecked(True)
                toggle.blockSignals(False)
            self._show_only_operations(DEFAULT_OPERATION_VISIBILITY)
            self._last_filter_signature = None
            self._apply_filters()
            if self.table.isRowHidden(row):
                self._info("Go to UID", f"Entry {source_line} is not shown: {entry.detail_text}")
                return True

        self.table.clearSelection()
        self.table.selectRow(row)

        focus_item = self.table.item(row, UID_COLUMN) or self.table.item(row, PATH_COLUMN)
        if focus_item:
            self.table.scrollToItem(focus_item, QAbstractItemView.ScrollHint.PositionAtCenter)
            focus_item.setSelected(True)
        return True

    def _prompt_go_to_uid(self) -> None:
        prefill = ""
        current_entry = self._entry_for_row(self.table.currentRow())
        if current_entry:
            prefill = str(current_entry.source_line)
        uid_text, ok = QInputDialog.getText(
            self,
            "Go to UID",
            "Enter UID (line number):",
            text=prefill,
        )
        if not ok:
            return
        uid_text = uid_text.strip()
        if not uid_text:
            return
        try:
            source_line = int(uid_text)
        except ValueError:
            self._warn("Go to UID", "UID must be a number.")
            return
        if source_line <= 0:
            self._warn("Go to UID", "UID must be positive.")
            return
        if self._go_to_uid(source_line):
            return
        self._info("Go to UID", f"No entry with UID {source_line} was found.")


    def _show_message(
        self,
        icon: QMessageBox.Icon,
        title: str,
        text: str,
    ) -> None:
        dialog = QMessageBox(self)
        dialog.setIcon(icon)
        dialog.setWindowTitle(title)
        dialog.setText(text)
        dialog.exec()

    def _open_external_path(
        self,
        path: Optional[Path],
        *,
        label: str,
        missing_message: Optional[str] = None,
        verb: str = "open",
    ) -> None:
        if not path:
            return
        if not path.exists():
            logger.warning("%s not found: %s", label, path)
            if missing_message:
                self._warn(label, missing_message)
            return
        try:
            os.startfile(path, verb)
            return
        except OSError as exc:
            logger.warning("Failed to open %s %s: %s", label.lower(), path, exc)
        with suppress(OSError):
            subprocess.Popen([str(NOTEPAD_PATH), str(path)])

    _info = partialmethod(_show_message, QMessageBox.Icon.Information)
    _warn = partialmethod(_show_message, QMessageBox.Icon.Warning)
    _critical = partialmethod(_show_message, QMessageBox.Icon.Critical)

    def _ensure_entries_present(self) -> bool:
        if self._entries:
            return True
        self._info("No entries", "Load a registry command list first.")
        return False

    def _cancel_worker(self, worker: Optional[_WorkerBase]) -> bool:
        if not self._is_running(worker):
            return False
        worker.cancel()
        return True


    def _choose_list_file(self) -> None:
        base_dir = self._list_path.parent if self._list_path else Path.cwd()
        start_dir = str(base_dir)
        file_name, _ = QFileDialog.getOpenFileName(
            self,
            "Select registry list",
            start_dir,
            SUPPORTED_LIST_FILTER,
        )
        if file_name:
            self._load_entries(Path(file_name))

    def _load_program_list(self) -> None:
        if self._load_default_list(force=True):
            return

        sample = SUPPORTED_LIST_DESC
        self._warn(
            "Load List",
            f"No {DEFAULT_LIST_BASENAME} file ({sample}) was found in the application directory.",
        )

    def _view_log(self) -> None:
        log_path = _default_log_path()
        self._open_external_path(log_path, label="Log", missing_message=f"Log file not found:\n{log_path}")

    def _reset_view(self) -> None:
        self._pending_window_size = QSize(*DEFAULT_WINDOW_SIZE)
        self._pending_window_pos = None
        self._apply_pending_window_size()
        self._column_widths = dict(DEFAULT_COLUMN_WIDTHS)
        self._apply_saved_column_widths()
        self._show_all_columns()
        self._apply_default_column_visibility()
        self._show_only_operations(DEFAULT_OPERATION_VISIBILITY)
        self._reset_state_toggles()
        self._inline_find_input.clear()
        self._refresh_filters()
        self._settings_save_timer.start(0)

    def _open_list_external(self) -> None:
        if not self._list_path:
            self._info("View List", "No list is currently loaded.")
            return
        verb = "edit" if self._list_path.suffix.lower() in (".bat", ".cmd") else "open"
        self._open_external_path(self._list_path, label="List", verb=verb)

    def _mark_access_denied(self, entry_ids: Sequence[str]) -> bool:
        if not entry_ids:
            return False
        denied = set(entry_ids)
        for entry in self._entries:
            if entry.unique_id in denied:
                entry.access_denied = True
        return True

    def _clear_access_denied(self) -> bool:
        cleared = False
        for entry in self._entries:
            if entry.access_denied:
                entry.access_denied = False
                cleared = True
        return cleared

    def _start_scan(self, *, manual: bool = True) -> None:
        if not self._ensure_entries_present():
            return
        if self._cancel_worker(self._scan_worker):
            self._update_action_states()
            return

        if manual and self._clear_access_denied():
            self._refresh_filters()

        scannable = [
            entry for entry in self._entries if not (entry.syntax_error or entry.conflict)
        ]
        self._scan_worker = self._launch_worker(
            ScanWorker(scannable),
            finished=self._scan_finished,
            failed=self._scan_failed,
        )

        self._begin_status("scan", "Scanning registry…", total=len(scannable))
        self._scan_completed = False
        self._update_action_states()

    def _scan_finished(self, results: list[ScanResult]) -> None:
        if self.sender() is not self._scan_worker:
            return
        self._scan_worker = None
        self._scan_completed = True
        self._merge_results(results)
        denied = sum(1 for entry in self._entries if entry.access_denied)
        summary, self._apply_summary = self._apply_summary, ""
        message = f"Execution complete: {summary}" if summary else "Scan completed"
        self._end_status("scan", message, extra=f"{denied} denied (hidden)" if denied else "")
        self._refresh_filters()

    def _scan_failed(self, message: str) -> None:
        if self.sender() is not self._scan_worker:
            return
        self._scan_worker = None
        self._apply_summary = ""
        if message == ScanWorker.CANCEL_MESSAGE:
            self._end_status("scan", "Scan cancelled, press F5 to rescan", state="ready")
            self._update_action_states()
            return
        self._end_status("scan", message, state="error")
        self._scan_completed = False
        self._update_action_states()
        self._warn("Scan Failed", message)

    def _merge_results(self, results: Iterable[ScanResult]) -> None:
        sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)
        self.table.setUpdatesEnabled(False)
        self.table.blockSignals(True)
        mismatches = 0
        try:
            for result in results:
                check_item = self._check_item_lookup.get(result.entry_id)
                if not check_item:
                    continue
                entry = check_item.data(self.ENTRY_ROLE)
                if not isinstance(entry, RegistryEntry):
                    continue
                changed = (
                    entry.actual != result.actual
                    or entry.compliant != result.compliant
                    or entry.detail != result.detail
                    or entry.type_mismatch != result.type_mismatch
                )
                entry.actual = result.actual
                entry.compliant = result.compliant
                entry.detail = result.detail
                entry.type_mismatch = result.type_mismatch
                entry.access_denied = entry.access_denied or result.access_denied
                if result.type_mismatch:
                    mismatches += 1
                if changed:
                    entry.refresh_search_blob()
                    self._apply_scan_state(check_item.row(), entry)
        finally:
            self.table.blockSignals(False)
            self.table.setSortingEnabled(sorting)
            if mismatches and self.table.horizontalHeader().sortIndicatorSection() < 0:
                self._sort_table(PATH_COLUMN, Qt.SortOrder.AscendingOrder)
            self.table.setUpdatesEnabled(True)
        if mismatches:
            logger.error("Scan found %d type mismatch(es) in %s", mismatches, self._list_path)

    def _with_reset_members(self, targets: Sequence[RegistryEntry]) -> tuple[list[RegistryEntry], int]:
        unwritable = {
            entry.unique_id
            for entry in self._entries
            if entry.list_error or entry.access_denied
        }
        queued: list[RegistryEntry] = []
        wanted: set[str] = set()
        unsafe_resets = 0
        for entry in targets:
            plan = entry.reset_plan
            if plan is not None:
                blocked = unwritable.intersection(plan.member_ids)
                if blocked:
                    unsafe_resets += 1
                    logger.warning(
                        "Skipping reset of %s: %d listed value(s) could not be rewritten afterwards",
                        entry.registry_path,
                        len(blocked),
                    )
                    continue
                wanted.update(plan.member_ids)
            queued.append(entry)
        if not wanted:
            return queued, unsafe_resets

        already_queued = {entry.unique_id for entry in queued}
        additions = [
            entry
            for entry in self._entries
            if entry.unique_id in wanted
            and entry.unique_id not in already_queued
            and not entry.list_error
            and not entry.access_denied
        ]
        if not additions:
            return queued, unsafe_resets

        order = {entry.unique_id: index for index, entry in enumerate(self._entries)}
        combined = sorted([*queued, *additions], key=lambda entry: order.get(entry.unique_id, 0))
        logger.info("Reset: rewriting %d listed value(s) after the key delete", len(additions))
        return combined, unsafe_resets

    def _start_apply(self, *, selected_only: bool) -> None:
        if not self._ensure_entries_present():
            return
        if self._cancel_worker(self._apply_worker):
            return

        noncompliant, selected_noncompliant = self._noncompliant_groups()
        target_entries, unsafe_resets = self._with_reset_members(
            selected_noncompliant if selected_only else noncompliant
        )
        blocked = self._list_error_count
        notes: list[str] = []
        if blocked:
            notes.append(
                f"{blocked} entry(s) with a LIST ERROR are excluded: bad syntax, lines that "
                "write different data to the same value, or a value type the registry does not "
                "use. Writing them could damage every machine the list runs on."
            )
        if unsafe_resets:
            notes.append(
                f"{unsafe_resets} reset key(s) are excluded: wiping them would destroy listed "
                "values that cannot be rewritten afterwards."
            )
        excluded = "\n\n".join(notes)

        if not target_entries:
            scope = "are selected" if selected_only else "were found"
            self._info(
                "Nothing to apply", f"No non-compliant entries {scope}.\n\n{excluded}".rstrip()
            )
            return

        if notes:
            confirm = QMessageBox(self)
            confirm.setIcon(QMessageBox.Icon.Warning)
            confirm.setWindowTitle("Entries Excluded")
            confirm.setText(f"{excluded}\n\nApply the remaining {len(target_entries)} entry(s)?")
            proceed = confirm.addButton("Apply Remaining", QMessageBox.ButtonRole.AcceptRole)
            confirm.addButton(QMessageBox.StandardButton.Cancel)
            confirm.exec()
            if confirm.clickedButton() is not proceed:
                return

        self._apply_worker = self._launch_worker(
            ApplyWorker(target_entries),
            finished=self._apply_finished,
            failed=self._apply_failed,
        )

        self._begin_status("apply", "Applying registry fixes…", total=len(target_entries))
        self._apply_in_progress = True
        self._update_action_states()

    def _apply_finished(self, outcome: ExecutionOutcome) -> None:
        self._apply_worker = None
        self._apply_in_progress = False
        if self._mark_access_denied(outcome.denied_ids):
            self._refresh_filters()
        parts = [f"Applied {outcome.succeeded}"]
        if outcome.failed:
            parts.append(f"Failed {outcome.failed}")
        if outcome.skipped:
            parts.append(f"Skipped {outcome.skipped}")
        extra = ", ".join(parts)
        logger.info("Execution complete: %s", extra)
        self._end_status("apply", "Execution complete", extra=extra)
        self._apply_summary = extra
        self._update_action_states()
        if outcome.failed:
            message = "\n".join(outcome.errors[:MAX_ERROR_DISPLAY])
            if outcome.failed > MAX_ERROR_DISPLAY:
                message += "\n…"
            self._warn(
                "Apply: Partial Failure",
                f"Success: {outcome.succeeded}\nFailed: {outcome.failed}\n\n{message}",
            )
        self._start_scan(manual=False)

    def _apply_failed(self, message: str) -> None:
        self._apply_worker = None
        if message == ApplyWorker.CANCEL_MESSAGE:
            self._end_status("apply", "Execution cancelled", state="ready")
            self._apply_in_progress = False
            self._update_action_states()
            return
        self._end_status("apply", message, state="error")
        self._warn("Execution Failed", message)
        self._apply_in_progress = False
        self._update_action_states()

    def _update_progress(self, processed: int, total: int) -> None:
        if self.sender() not in (self._scan_worker, self._apply_worker):
            return
        self._progress.setRange(0, max(1, total))
        self._animate_progress(processed)

    def _export_csv(self) -> None:
        if not self._ensure_entries_present():
            return

        entries_to_export = [entry for entry in self._entries if entry.compliant is not True]

        default_name = f"{self._list_path.stem}_results.csv" if self._list_path else "registry_results.csv"
        default_dir = self._list_path.parent if self._list_path else Path.home()
        file_name, _ = QFileDialog.getSaveFileName(
            self,
            "Export CSV",
            str(default_dir / default_name),
            "CSV files (*.csv)",
        )
        if not file_name:
            return
        try:
            with open(file_name, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(
                    ["UID", "Path", "Value", "Type", "Expected", "Actual", "Compliant", "Detail", "Command"]
                )
                for entry in entries_to_export:
                    compliance = (
                        "SYNTAX ERROR"
                        if entry.syntax_error
                        else "LIST CONFLICT"
                        if entry.conflict
                        else "TYPE MISMATCH"
                        if entry.type_mismatch
                        else "ACCESS DENIED"
                        if entry.access_denied
                        else COMPLIANCE_TEXT.get(entry.compliant, "Unknown")
                    )
                    writer.writerow([
                        entry.source_line,
                        *(_csv_safe(field) for field in (
                            entry.full_path,
                            entry.display_name,
                            entry.value_type.value,
                            entry.expected_display,
                            entry.actual_display,
                            compliance,
                            entry.detail_text,
                            entry.raw_command,
                        )),
                    ])
        except OSError as exc:
            self._critical("Export", f"Failed to export CSV:\n{exc}")
            return
        self._set_status("success", f"Exported {len(entries_to_export)} entries to CSV")

    def _show_table_menu(self, pos: QPoint) -> None:
        index = self.table.indexAt(pos)
        row = index.row()
        if row < 0:
            return
        entry = self._entry_for_row(row)
        if not entry:
            return

        menu = QMenu(self)
        detail_item = self.table.item(row, DETAILS_COLUMN)
        detail_text = detail_item.text() if detail_item else entry.detail_text
        action_map: dict[QAction, Callable[[], None]] = {
            menu.addAction("Copy Path"): lambda: self._copy_to_clipboard(entry.full_path),
            menu.addAction("Copy Name"): lambda: self._copy_to_clipboard(entry.display_name),
            menu.addAction("Copy compliance details"): lambda: self._copy_to_clipboard(detail_text),
            menu.addAction("Copy reg command"): lambda: self._copy_to_clipboard(entry.raw_command),
        }
        if entry.hive:
            menu.addSeparator()
            action_map[menu.addAction("Jump to registry")] = lambda: self._jump_to_registry(entry)

        chosen = menu.exec(self.table.viewport().mapToGlobal(pos))
        if chosen in action_map:
            action_map[chosen]()

    @staticmethod
    def _copy_to_clipboard(text: str) -> None:
        if text:
            QApplication.clipboard().setText(text)

    def _jump_to_registry(self, entry: RegistryEntry) -> None:
        try:
            subprocess.run(
                [str(TASKKILL_PATH), "/IM", "regedit.exe", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=TASKKILL_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Unable to close Regedit before navigating: %s", exc)
        QTimer.singleShot(350, lambda: self._open_regedit_at(entry))

    def _open_regedit_at(self, entry: RegistryEntry) -> None:
        try:
            with winreg.CreateKeyEx(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Applets\Regedit",
                0,
                REG_WRITE_64,
            ) as reg_key:
                winreg.SetValueEx(reg_key, "LastKey", 0, winreg.REG_SZ, f"Computer\\{entry.registry_path}")
        except OSError as exc:
            self._warn("Registry", f"Failed to set Regedit navigation key:\n{exc}")
            return

        launched = _shell_execute(None, "open", str(REGEDIT_PATH), None, None, SW_SHOWNORMAL)
        if launched <= SHELL_EXECUTE_ERROR_THRESHOLD:
            self._warn("Registry", f"Failed to launch Regedit (ShellExecuteW returned {launched}).")

    def _drain_live_workers(self) -> None:
        for worker in list(self._live_workers):
            try:
                if worker.isRunning():
                    worker.cancel()
                    if not worker.wait(WORKER_WAIT_TIMEOUT_MS):
                        logger.warning(
                            "%s did not stop within %d ms; waiting for it to finish",
                            type(worker).__name__,
                            WORKER_WAIT_TIMEOUT_MS,
                        )
                        worker.wait()
            except RuntimeError:
                pass
            self._live_workers.discard(worker)


    def closeEvent(self, event) -> None:
        self._drain_live_workers()
        self._save_last_session()
        super().closeEvent(event)


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except OSError:
        return False


def _show_admin_prompt() -> str:
    msg = QMessageBox()
    msg.setWindowTitle("Administrator Privileges Recommended")
    msg.setIcon(QMessageBox.Icon.Warning)
    msg.setText(
        "This application may modify system registry keys (e.g., HKLM).\n"
        "Administrator privileges are recommended.\n\nRun as administrator now?"
    )
    run_btn = msg.addButton("Run as Administrator", QMessageBox.ButtonRole.YesRole)
    msg.addButton("Continue Without", QMessageBox.ButtonRole.NoRole)
    exit_btn = msg.addButton("Exit", QMessageBox.ButtonRole.RejectRole)
    screen = QApplication.primaryScreen()
    if screen is not None:
        geo = screen.availableGeometry()
        hint = msg.sizeHint()
        msg.move(geo.x() + (geo.width() - hint.width()) // 2, geo.y() + (geo.height() - hint.height()) // 2)
    msg.exec()
    clicked = msg.clickedButton()
    if clicked == run_btn:
        return "elevate"
    if clicked == exit_btn:
        return "exit"
    return "continue"


def _request_elevation() -> bool:
    executable = sys.executable
    if getattr(sys, "frozen", False):
        params = " ".join(f'"{arg}"' for arg in sys.argv[1:])
    else:
        script = Path(sys.argv[0]).resolve()
        params = " ".join([f'"{script}"'] + [f'"{arg}"' for arg in sys.argv[1:]])
    ret = _shell_execute(None, "runas", executable, params or None, str(Path.cwd()), SW_SHOWNORMAL)
    if ret <= SHELL_EXECUTE_ERROR_THRESHOLD:
        raise OSError(f"ShellExecuteW failed with code {ret}")
    return True


def main() -> int:
    init_logging()

    QApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )

    app = QApplication(sys.argv)
    app.setApplicationName("Registry Sentinel")
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName("ClementineDev")
    app.setFont(FONT_SEGOE)
    app.setStyleSheet(STYLESHEET)
    app.setWindowIcon(app.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon))

    if not _is_admin():
        choice = _show_admin_prompt()
        if choice == "elevate":
            try:
                _request_elevation()
                logger.info("Elevation requested; exiting current instance.")
                logging.shutdown()
                return 0
            except OSError as exc:
                logger.error("Failed to relaunch as administrator: %s", exc)
                QMessageBox.critical(
                    None,
                    "Elevation Failed",
                    f"Failed to relaunch as administrator:\n{exc}\n\nContinuing without elevation.",
                )
        elif choice == "exit":
            logger.info("User opted to exit without elevation.")
            logging.shutdown()
            return 0
        else:
            logger.info("Continuing without elevation; some operations may require admin rights.")

    window = RegistrySentinel()
    window.show()

    result = app.exec()
    logging.shutdown()
    return int(result)


if __name__ == "__main__":
    sys.exit(main())
