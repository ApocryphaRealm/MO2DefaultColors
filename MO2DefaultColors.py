"""MO2 Default Colors - a Mod Organizer 2 plugin: a default colour for separators and one for mods, run from the
mod list's right-click menu, beside MO2's own "Select Color..." and "Reset Color".

The owner, 2026-09-22, after generated separators came up in MO2's grey: "create a plugin that lets you set a default
color for separators and mods" - and "I don't want a toolbar pop up. I want you to make it so that it's paired with
the right click a mod or separator context menu and right next to the reset color and choose color".

Right-click a separator (or a mod) and the menu gains, next to Select Color / Reset Color:
  * Use This Color as the Default for Separators|Mods  - the clicked entry's colour becomes the default
  * Choose Default Color for Separators|Mods...        - a colour picker for the default
  * Apply Default Color                                 - to the selected entries
  * Apply Default Color to All Separators|Mods
  * Color New Separators|Mods Automatically             - checkable; a new entry without a colour gets the default
                                                          on the next refresh
MO2's Python API reads a mod's colour but cannot set it, so the colour is written the way MO2 stores it - the color=
line of meta.ini, a QColor in QSettings' @Variant encoding - and MO2 is refreshed. No toolbar button, no window of
its own; the hook is an event filter on the mod list view alone (never application-wide).

Copyright (C) 2026 ApocryphaRealm. GPL-3.0-or-later - see LICENSE and NOTICE.md.
"""
__version__ = "1.0.0"

import os
import time

import mobase

try:
    from PyQt6.QtCore import QEvent, QObject, Qt, QTimer
    from PyQt6.QtGui import QAction, QColor
    from PyQt6.QtWidgets import QApplication, QColorDialog, QMenu, QTreeView
except ImportError:
    from PyQt5.QtCore import QEvent, QObject, Qt, QTimer
    from PyQt5.QtGui import QColor
    from PyQt5.QtWidgets import QAction, QApplication, QColorDialog, QMenu, QTreeView


# --- the colour line, as MO2 writes it -------------------------------------------------------------------------------
def _escape(data: bytes) -> str:
    """The escaping MO2's meta.ini shows for a QColor variant: a zero byte as backslash-0, every other byte as
    backslash-x and its hex digits (MO2 writes the 0x43 type byte that way too, never as the printable 'C'), so the
    line produced here matches MO2's own byte for byte."""
    return "".join("\\0" if b == 0 else "\\x%x" % b for b in data)


def _unescape(text: str) -> bytes:
    out = bytearray()
    i = 0
    while i < len(text):
        c = text[i]
        if c == "\\" and i + 1 < len(text):
            n = text[i + 1]
            if n == "0":
                out.append(0)
                i += 2
                continue
            if n == "x":
                j = i + 2
                while j < len(text) and j < i + 4 and text[j] in "0123456789abcdefABCDEF":
                    j += 1
                out.append(int(text[i + 2:j], 16))
                i = j
                continue
            out.append(ord(n))
            i += 2
            continue
        out.append(ord(c))
        i += 1
    return bytes(out)


def color_line(c: QColor) -> str:
    """color=@Variant(<QColor>) - type 67 (QColor), spec 1 (RGB), then alpha, red, green, blue as 16-bit values, then
    two bytes of padding. Black is \\0\\0\\0\\x43\\x1\\xff\\xff\\0\\0\\0\\0\\0\\0\\0\\0, which is what every one of
    the owner's own separators carried."""
    def u16(v):
        v = v * 257
        return bytes(((v >> 8) & 0xFF, v & 0xFF))
    payload = b"\x00\x00\x00\x43" + b"\x01" + u16(c.alpha()) + u16(c.red()) + u16(c.green()) + u16(c.blue()) + b"\x00\x00"
    return "color=@Variant(" + _escape(payload) + ")"


def color_from_line(line: str):
    """The QColor a color= line encodes, or None (no line, or not an RGB QColor)."""
    if not line or not line.startswith("color=@Variant(") or not line.endswith(")"):
        return None
    raw = _unescape(line[len("color=@Variant("):-1])
    if len(raw) < 13 or raw[3] != 0x43 or raw[4] != 1:
        return None
    a, r, g, b = (raw[5] << 8 | raw[6]), (raw[7] << 8 | raw[8]), (raw[9] << 8 | raw[10]), (raw[11] << 8 | raw[12])
    return QColor(r >> 8, g >> 8, b >> 8, a >> 8)


def read_color_line(meta_path: str):
    try:
        for line in open(meta_path, encoding="utf-8", errors="ignore"):
            if line.startswith("color="):
                return line.rstrip("\r\n")
    except OSError:
        pass
    return None


def write_color_line(meta_path: str, line: str) -> bool:
    try:
        text = open(meta_path, "rb").read().decode("utf-8-sig") if os.path.isfile(meta_path) else "[General]\n"
    except OSError:
        return False
    nl = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(nl)
    for i, l in enumerate(lines):
        if l.startswith("color="):
            lines[i] = line
            break
    else:
        if "[General]" in lines:
            lines.insert(lines.index("[General]") + 1, line)
        else:
            lines[0:0] = ["[General]", line]
    try:
        open(meta_path, "wb").write(nl.join(lines).encode("utf-8"))
        return True
    except OSError:
        return False


def is_separator(name: str) -> bool:
    return name.endswith("_separator")


# --- the context-menu hook: a filter on the mod list view only ---------------------------------------------------------
class _MenuHook(QObject):
    """MO2 builds the mod list's menu inside its own context-menu handling, so the popup does not exist while the
    event is seen; the append is queued for the moment it does. Nothing MO2 built is removed or reordered."""

    def __init__(self, view, build):
        super().__init__(view)
        self._view = view
        self._build = build

    def eventFilter(self, obj, event):
        try:
            if event.type() == QEvent.Type.ContextMenu:
                QTimer.singleShot(0, self._append)
        except Exception:  # noqa: BLE001
            pass
        return False

    def _selected(self):
        sm = self._view.selectionModel()
        if sm is None:
            return []
        names = []
        for index in sm.selectedRows():
            value = index.data(Qt.ItemDataRole.DisplayRole)
            if value:
                names.append(str(value))
        return names

    def _append(self):
        menu = QApplication.activePopupWidget()
        if not isinstance(menu, QMenu):
            return
        try:
            self._build(self._selected(), menu)
        except Exception:  # noqa: BLE001 - never break MO2's own menu
            pass


# --- the plugin --------------------------------------------------------------------------------------------------------
class MO2DefaultColors(mobase.IPlugin):
    def __init__(self):
        super().__init__()
        self._organizer = None
        self._hook = None
        self._busy = False

    def init(self, organizer):
        self._organizer = organizer
        try:
            organizer.onUserInterfaceInitialized(self._ui_ready)
        except Exception as exc:  # noqa: BLE001
            self._log(f"onUserInterfaceInitialized: {exc!r}")
        try:
            organizer.modList().onRefreshed(self._on_refreshed)
        except Exception as exc:  # noqa: BLE001
            self._log(f"onRefreshed not available: {exc!r}")
        return True

    def name(self):
        return "MO2 Default Colors"

    def author(self):
        return "ApocryphaRealm"

    def description(self):
        return ("A default colour for separators and one for mods, set and applied from the mod list's right-click menu "
                "beside Select Color / Reset Color; new entries without a colour get the default automatically.")

    def version(self):
        return mobase.VersionInfo(1, 0, 0, mobase.ReleaseType.FINAL)

    def isActive(self):
        return True

    def settings(self):
        return [
            mobase.PluginSetting("separator_color", "Default separator colour (#rrggbb); empty = none", "#000000"),
            mobase.PluginSetting("mod_color", "Default mod colour (#rrggbb); empty = none", ""),
            mobase.PluginSetting("auto_separators", "Give new separators the default colour automatically", True),
            mobase.PluginSetting("auto_mods", "Give new mods the default colour automatically", False),
        ]

    # settings
    def _get(self, key, default):
        try:
            v = self._organizer.pluginSetting(self.name(), key)
            return default if v is None else v
        except Exception:  # noqa: BLE001
            return default

    def _set(self, key, value):
        try:
            self._organizer.setPluginSetting(self.name(), key, value)
        except Exception as exc:  # noqa: BLE001
            self._log(f"setting {key}: {exc!r}")

    def _default(self, separators: bool):
        text = str(self._get("separator_color" if separators else "mod_color", "")).strip()
        c = QColor(text) if text else QColor()
        return c if c.isValid() else None

    # the menu
    def _ui_ready(self, window):
        view = window.findChild(QTreeView, "modList")
        if view is None:
            self._log("modList view not found; no menu entries")
            return
        self._hook = _MenuHook(view, self._build_menu)
        view.viewport().installEventFilter(self._hook)
        view.installEventFilter(self._hook)
        self._log("context-menu entries installed")

    def _build_menu(self, names, menu):
        if not names:
            return
        seps = is_separator(names[0])
        kind = "Separators" if seps else "Mods"
        names = [n for n in names if is_separator(n) == seps]
        # next to MO2's own colour entries: after Reset Color when it is there, else after Select Color...
        anchor = None
        for a in menu.actions():
            t = (a.text() or "").replace("&", "")
            if t.startswith("Select Color"):
                anchor = a
            elif t.startswith("Reset Color"):
                anchor = a
                break
        following = None
        if anchor is not None:
            acts = menu.actions()
            i = acts.index(anchor)
            following = acts[i + 1] if i + 1 < len(acts) else None

        def add(text, slot, checkable=False, checked=False, enabled=True):
            act = QAction(text, menu)
            act.setCheckable(checkable)
            act.setChecked(checked)
            act.setEnabled(enabled)
            act.triggered.connect(slot)
            if following is not None:
                menu.insertAction(following, act)
            else:
                menu.addAction(act)
            return act

        default = self._default(seps)
        shown = default.name() if default is not None else "none"
        clicked = names[0]
        clicked_colour = color_from_line(read_color_line(os.path.join(self._organizer.modsPath(), clicked, "meta.ini")))
        sep0 = QAction(menu)
        sep0.setSeparator(True)
        menu.insertAction(following, sep0) if following is not None else menu.addAction(sep0)
        add(f"Use This Color as the Default for {kind}", lambda _c=False: self._use_as_default(seps, clicked_colour),
            enabled=clicked_colour is not None)
        add(f"Choose Default Color for {kind}... (now {shown})", lambda _c=False: self._choose_default(seps))
        add("Apply Default Color", lambda _c=False, ns=tuple(names): self._apply_to(seps, ns), enabled=default is not None)
        add(f"Apply Default Color to All {kind}", lambda _c=False: self._apply_to(seps, None), enabled=default is not None)
        add(f"Color New {kind} Automatically", lambda checked=False: self._set("auto_separators" if seps else "auto_mods", bool(checked)),
            checkable=True, checked=bool(self._get("auto_separators" if seps else "auto_mods", seps)))
        sep1 = QAction(menu)
        sep1.setSeparator(True)
        menu.insertAction(following, sep1) if following is not None else menu.addAction(sep1)

    # actions
    def _use_as_default(self, seps, colour):
        if colour is None:
            return
        self._set("separator_color" if seps else "mod_color", colour.name())
        self._log(f"default {'separator' if seps else 'mod'} colour = {colour.name()} (from the clicked entry)")

    def _choose_default(self, seps):
        start = self._default(seps) or QColor("#000000")
        c = QColorDialog.getColor(start, None, f"Default color for {'separators' if seps else 'mods'}")
        if c.isValid():
            self._set("separator_color" if seps else "mod_color", c.name())
            self._log(f"default {'separator' if seps else 'mod'} colour = {c.name()}")

    def _targets(self, seps, only_missing):
        ml = self._organizer.modList()
        mods_dir = self._organizer.modsPath()
        out = []
        for name in ml.allMods():
            if is_separator(name) != seps:
                continue
            meta = os.path.join(mods_dir, name, "meta.ini")
            if only_missing and read_color_line(meta):
                continue
            out.append((name, meta))
        return out

    def _apply_to(self, seps, names):
        """names: the selection, or None for every separator/mod."""
        default = self._default(seps)
        if default is None:
            return
        line = color_line(default)
        mods_dir = self._organizer.modsPath()
        targets = [(n, os.path.join(mods_dir, n, "meta.ini")) for n in names] if names else self._targets(seps, only_missing=False)
        done = 0
        for _n, meta in targets:
            if read_color_line(meta) != line and write_color_line(meta, line):
                done += 1
        self._log(f"applied the default {'separator' if seps else 'mod'} colour {default.name()} to {done} of {len(targets)}")
        self._organizer.refresh(True)

    def _on_refreshed(self):
        if self._busy:
            return
        self._busy = True
        try:
            n = 0
            for seps, key in ((True, "auto_separators"), (False, "auto_mods")):
                if not self._get(key, seps):
                    continue
                default = self._default(seps)
                if default is None:
                    continue
                line = color_line(default)
                for _name, meta in self._targets(seps, only_missing=True):
                    if write_color_line(meta, line):
                        n += 1
            if n:
                self._log(f"gave the default colour to {n} new entr{'y' if n == 1 else 'ies'}")
                QTimer.singleShot(0, lambda: self._organizer.refresh(True))
        finally:
            self._busy = False

    def _log(self, msg):
        try:
            d = os.path.join(self._organizer.basePath(), "plugins", "data", "MO2DefaultColors")
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "log.txt"), "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%H:%M:%S ") + msg + "\n")
        except Exception:  # noqa: BLE001
            pass


def createPlugin():
    return MO2DefaultColors()
