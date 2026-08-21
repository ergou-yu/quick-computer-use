"""Role and value normalization across platforms.

The three control surfaces (Web ARIA, macOS AX, Windows UIA) all have their
own role vocabularies. The Router and LLM see one normalized set.
"""

from __future__ import annotations

import re
from typing import Any, Optional


def wait_ms(params: Any) -> int:
    """Resolve a ``wait`` action's duration to milliseconds.

    Accepts ``{ms}``, ``{seconds}``, or ``{s}`` (the latter two multiplied by
    1000). Default is 100 ms to preserve historical behavior. This fixes the
    field report where ``{"seconds": 2}`` silently fell through to the 100 ms
    default because only ``ms`` was read — a natural unit mismatch between
    LLMs and the original schema.

    The three layer backends (desktop_ax / desktop_appleevents / web_a11y)
    share this single resolver so the unit semantics can never drift.
    """
    p = params or {}
    if not isinstance(p, dict):
        return 100
    if p.get("ms") is not None:
        try:
            return max(0, int(float(p["ms"])))
        except (TypeError, ValueError):
            pass
    for key in ("seconds", "s"):
        v = p.get(key)
        if v is not None:
            try:
                return max(0, int(float(v) * 1000))
            except (TypeError, ValueError):
                pass
    return 100


# ---------------------------------------------------------------------------
# Normalized roles
# ---------------------------------------------------------------------------
# A small, opinionated set that covers ~95% of automation targets.
# Anything we can't map falls into "other" and is exposed with its raw role.

NORMALIZED_ROLES = frozenset(
    {
        "button",
        "link",
        "text",
        "edit",  # text input
        "password",
        "checkbox",
        "radio",
        "combobox",  # <select>
        "listbox",  # multi-select
        "option",  # item inside a select / listbox
        "list",
        "list_item",
        "tree",
        "tree_item",
        "menu",
        "menu_item",
        "menu_bar",
        "tab",
        "tab_item",
        "tab_panel",
        "dialog",
        "alert",
        "tooltip",
        "window",
        "pane",
        "image",
        "slider",
        "progress",
        "spinbutton",  # numeric input
        "switch",
        "toggle",
        "search",
        "searchbox",
        "heading",
        "separator",
        "group",
        "application",
        "document",
        "form",
        "region",
        "table",
        "row",
        "cell",
        "columnheader",
        "rowheader",
        "navigation",
        "main",
        "banner",
        "contentinfo",
        "complementary",
        "img",  # synonym for image
        "video",
        "audio",
        "canvas",
    }
)


# Roles the user typically wants to click/type into. Shared by the feature
# extractor and every layer so the "interactive" definition can't drift.
# Kept next to NORMALIZED_ROLES so any new interactive role is added once.
INTERACTIVE_ROLES = frozenset(
    {
        "button",
        "link",
        "edit",
        "password",
        "searchbox",
        "checkbox",
        "radio",
        "switch",
        "combobox",
        "listbox",
        "option",
        "menu_item",
        "tab",
        "tab_item",
        "slider",
        "spinbutton",
        "tree_item",
    }
)


# ---------------------------------------------------------------------------
# ARIA → normalized  (web)
# ---------------------------------------------------------------------------

_ARIA_MAP: dict[str, str] = {
    "button": "button",
    "link": "link",
    "textbox": "edit",
    "searchbox": "searchbox",
    "checkbox": "checkbox",
    "radio": "radio",
    "switch": "switch",
    "combobox": "combobox",
    "listbox": "listbox",
    "option": "option",
    "list": "list",
    "listitem": "list_item",
    "tree": "tree",
    "treeitem": "tree_item",
    "menu": "menu",
    "menuitem": "menu_item",
    "menubar": "menu_bar",
    "tab": "tab",
    "tabpanel": "tab_panel",
    "dialog": "dialog",
    "alertdialog": "alert",
    "tooltip": "tooltip",
    "window": "window",
    "img": "image",
    "image": "image",
    "slider": "slider",
    "progressbar": "progress",
    "spinbutton": "spinbutton",
    "heading": "heading",
    "separator": "separator",
    "group": "group",
    "application": "application",
    "document": "document",
    "form": "form",
    "region": "region",
    "table": "table",
    "row": "row",
    "cell": "cell",
    "columnheader": "columnheader",
    "rowheader": "rowheader",
    "navigation": "navigation",
    "main": "main",
    "banner": "banner",
    "contentinfo": "contentinfo",
    "complementary": "complementary",
    "video": "video",
    "audio": "audio",
    "canvas": "canvas",
    "StaticText": "text",
    "generic": "group",
    "InlineTextBox": "text",
    "LineBreak": "separator",
    "Heading": "heading",
    "LabelText": "text",
}


def from_aria(role: Optional[str]) -> str:
    if not role:
        return "group"
    r = _ARIA_MAP.get(role)
    if r:
        return r
    # Strip "AX" prefix used by Playwright for some elements.
    if role.startswith("AX"):
        return from_ax(role)
    # Case-insensitive fallback.
    rl = role.lower()
    return _ARIA_MAP.get(rl, "other")


# ---------------------------------------------------------------------------
# macOS AX → normalized
# ---------------------------------------------------------------------------

_AX_MAP: dict[str, str] = {
    "AXButton": "button",
    "AXLink": "link",
    "AXStaticText": "text",
    "AXTextField": "edit",
    "AXSecureTextField": "password",
    "AXTextArea": "edit",  # TextEdit/Notes body — the main text-editing surface
    "AXCheckBox": "checkbox",
    "AXRadioButton": "radio",
    "AXPopUpButton": "combobox",
    "AXComboBox": "combobox",
    "AXList": "list",
    "AXOutline": "tree",
    "AXTable": "table",
    "AXRow": "row",
    "AXColumn": "cell",
    "AXCell": "cell",
    "AXMenu": "menu",
    "AXMenuBar": "menu_bar",
    "AXMenuItem": "menu_item",
    "AXMenuButton": "button",
    "AXTabGroup": "tab",
    "AXTabButton": "tab_item",
    "AXDialog": "dialog",
    "AXSheet": "dialog",
    "AXWindow": "window",
    "AXImage": "image",
    "AXSlider": "slider",
    "AXProgressIndicator": "progress",
    "AXIncrementor": "spinbutton",
    "AXValueIndicator": "progress",
    "AXHeading": "heading",
    "AXHorizontalSplitter": "separator",
    "AXVerticalSplitter": "separator",
    "AXSplitGroup": "group",
    "AXGroup": "group",
    "AXApplication": "application",
    "AXDocument": "document",
    "AXLayoutArea": "region",
    "AXLayoutItem": "region",
    "AXWebArea": "document",
    "AXBrowser": "application",
    "AXToolbar": "pane",
    "AXDrawer": "pane",
    "AXGenericElement": "group",
    "AXSearchField": "searchbox",
    "AXHelpTag": "tooltip",
    "AXNavigationBar": "navigation",
    "AXMatte": "pane",
}


def from_ax(role: Optional[str]) -> str:
    if not role:
        return "group"
    return _AX_MAP.get(role, "other")


# ---------------------------------------------------------------------------
# Windows UIA → normalized
# ---------------------------------------------------------------------------

_UIA_MAP: dict[str, str] = {
    "ButtonControl": "button",
    "HyperlinkControl": "link",
    "TextControl": "text",
    "EditControl": "edit",
    "PasswordControl": "password",
    "CheckBoxControl": "checkbox",
    "RadioButtonControl": "radio",
    "ComboBoxControl": "combobox",
    "ListControl": "list",
    "ListItemControl": "list_item",
    "TreeControl": "tree",
    "TreeItemControl": "tree_item",
    "MenuControl": "menu",
    "MenuBarControl": "menu_bar",
    "MenuItemControl": "menu_item",
    "TabControl": "tab",
    "TabItemControl": "tab_item",
    "WindowControl": "window",
    "PaneControl": "pane",
    "ImageControl": "image",
    "SliderControl": "slider",
    "ProgressBarControl": "progress",
    "SpinnerControl": "spinbutton",
    "DataGridControl": "table",
    "DataItemControl": "row",
    "TableControl": "table",
    "DocumentControl": "document",
    "SemanticZoomControl": "region",
    "GroupControl": "group",
    "SplitButtonControl": "button",
    "HeaderControl": "pane",
    "HeaderItemControl": "tab_item",
    "StatusBarControl": "pane",
    "ToolBarControl": "pane",
    "ToolTipControl": "tooltip",
    "CustomControl": "group",
    "ThumbControl": "slider",
    "ScrollBarControl": "separator",
    "CalendarControl": "combobox",
    "AppBarControl": "pane",
}


def from_uia(control_type: Optional[str]) -> str:
    if not control_type:
        return "group"
    # uiautomation library sometimes appends "ControlTypeId" or returns int ids.
    name = control_type
    if name.endswith("ControlTypeId"):
        name = name[: -len("ControlTypeId")] + "Control"
    return _UIA_MAP.get(name, "other")


# ---------------------------------------------------------------------------
# Generic entry point
# ---------------------------------------------------------------------------


def normalize(role: Optional[str], source: str = "auto") -> str:
    """Normalize a raw role string.

    ``source`` is one of "aria", "ax", "uia", or "auto" (tries aria first,
    then ax, then uia, then lower-case substring fallback).
    """
    if not role:
        return "group"
    if source == "aria":
        return from_aria(role)
    if source == "ax":
        return from_ax(role)
    if source == "uia":
        return from_uia(role)
    # auto
    if role in _ARIA_MAP:
        return _ARIA_MAP[role]
    if role in _AX_MAP:
        return _AX_MAP[role]
    if role in _UIA_MAP:
        return _UIA_MAP[role]
    # Case-insensitive match across all three tables.
    rl = role.lower()
    for table in (_ARIA_MAP, _AX_MAP, _UIA_MAP):
        if rl in table:
            return table[rl]
    # Strip "AX" prefix used by Playwright for some elements.
    if role.startswith("AX"):
        r = from_ax(role)
        if r != "other":
            return r
    # Generic lowercased aria fallback.
    r = from_aria(rl)
    if r != "other":
        return r
    return "other"


# ---------------------------------------------------------------------------
# Value helpers
# ---------------------------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def clean_text(s: Optional[str], max_len: int = 200) -> str:
    """Collapse whitespace and truncate. Empty → ""."""
    if not s:
        return ""
    s = _WHITESPACE_RE.sub(" ", s).strip()
    if len(s) > max_len:
        s = s[: max_len - 1] + "…"
    return s