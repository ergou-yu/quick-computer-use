"""Quartz-based keyboard input helper.

Why this exists beyond ``desktop_ax._post_key_code``:
- The AppleScript ``keystroke``/``key code`` path is rejected by System Events
  in osascript-subprocess mode with "osascript is not allowed to send
  keystrokes. (1002)". The fix is to stop using System Events for HID
  synthesis and call ``CGEventCreateKeyboardEvent + CGEventPost`` directly.
- TCC requirement for this path is **only** ``kTCCServicePostEvent`` (Input
  Monitoring), not Accessibility and not Automation. Most QCU installs are
  already provisioned for Input Monitoring because accessibility screenshots
  need it; otherwise the user grants it once.
- A single ``CGEventKeyboardSetUnicodeString`` path handles arbitrary text
  (incl. non-ASCII / emoji) without per-letter keycode lookups.

This helper is imported from both desktop_ax and desktop_appleevents so
keystroke behavior can't drift between layers.
"""

from __future__ import annotations

from typing import Optional


# macOS virtual keycodes for the named special keys. Same data as
# ``desktop_ax._KEYCODES`` and ``desktop_appleevents._KEYNAME_TO_CODE``;
# consolidated here so callers can choose one stable import.
KEYCODES: dict[str, int] = {
    "return": 36, "enter": 36,
    "tab": 48,
    "space": 49,
    "delete": 51, "backspace": 51,
    "escape": 53, "esc": 53,
    "forwarddelete": 117, "fwddelete": 117,
    "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121,
    "up": 126, "uparrow": 126,
    "down": 125, "downarrow": 125,
    "left": 123, "leftarrow": 123,
    "right": 124, "rightarrow": 124,
    "help": 114,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
    "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
    # Letters get filled lazily via _letter_keycodes() so the literal here
    # doesn't drift from Carbon/HIToolbox/Events.h.
    "v": 9, "c": 8, "x": 7, "a": 0, "q": 12, "w": 13, "z": 6,
}


# Letter keycodes (A=0 ... Z=6). Carbon/HIToolbox/Events.h. kVK_ANSI_*.
_LETTER_KEYCODES: dict[str, int] = dict(
    a=0, b=11, c=8, d=2, e=14, f=3, g=5, h=4, i=34, j=38, k=40, l=37,
    m=46, n=45, o=31, p=35, q=12, r=15, s=1, t=17, u=32, v=9, w=13,
    x=7, y=16, z=6,
)
KEYCODES.update(_LETTER_KEYCODES)


# Symbol keycodes — the ones most often used in shortcuts (Cmd+[ etc.).
_SYMBOL_KEYCODES: dict[str, int] = {
    "[": 33, "]": 30, ",": 43, ".": 47, "/": 44,
    "-": 27, "=": 24, "`": 50, "\\": 42, ";": 41, "'": 39,
    "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23,
    "6": 22, "7": 26, "8": 28, "9": 25,
}
KEYCODES.update(_SYMBOL_KEYCODES)


# Modifier → bit mask (kCGEventFlagMask*).
MODIFIER_FLAGS: dict[str, int] = {
    # filled in lazily because Quartz import is module-level below
}


def _load_modifier_flags() -> dict[str, int]:
    """Resolve kCGEventFlagMask* at runtime so this module is importable on
    systems where PyObjC's Quartz sub-namespace puts these constants somewhere
    slightly different across versions.
    """
    try:
        from Quartz import (
            kCGEventFlagMaskCommand,
            kCGEventFlagMaskShift,
            kCGEventFlagMaskAlternate,
            kCGEventFlagMaskControl,
            kCGEventFlagMaskSecondaryFn,
        )
    except Exception:
        # Hard-coded fallbacks (these values have been constant on macOS for
        # over a decade). Useful when PyObjC is partially broken.
        return {
            "cmd": 1 << 20,
            "command": 1 << 20,
            "shift": 1 << 17,
            "alt": 1 << 19,
            "option": 1 << 19,
            "opt": 1 << 19,
            "ctrl": 1 << 18,
            "control": 1 << 18,
            "fn": 1 << 23,
        }
    return {
        "cmd": kCGEventFlagMaskCommand,
        "command": kCGEventFlagMaskCommand,
        "shift": kCGEventFlagMaskShift,
        "alt": kCGEventFlagMaskAlternate,
        "option": kCGEventFlagMaskAlternate,
        "opt": kCGEventFlagMaskAlternate,
        "ctrl": kCGEventFlagMaskControl,
        "control": kCGEventFlagMaskControl,
        "fn": kCGEventFlagMaskSecondaryFn,
    }


def parse_key_combo(key: str) -> tuple[Optional[int], int]:
    """Parse "Cmd+Shift+3" → (keycode, modifier_flags).

    Modifier aliases: Cmd/Command, Shift, Alt/Option/Opt, Ctrl/Control, Fn.
    Returns ``(None, 0)`` when the primary key can't be resolved.
    """
    if not key:
        return None, 0
    if not MODIFIER_FLAGS:
        MODIFIER_FLAGS.update(_load_modifier_flags())
    parts = [p.strip() for p in key.split("+")]
    mods = 0
    primary: Optional[str] = None
    for p in parts:
        lp = p.lower()
        if lp in MODIFIER_FLAGS:
            mods |= MODIFIER_FLAGS[lp]
        else:
            primary = lp
    if primary is None:
        return None, mods
    code = KEYCODES.get(primary)
    return code, mods


def type_text(text: str, *, delay_ms: int = 0) -> bool:
    """Type ``text`` char-by-char via ``CGEventKeyboardSetUnicodeString``.

    This is the path for arbitrary Unicode input; it bypasses the per-letter
    keycode table completely and lets Carbon resolve the layout. Works for
    ASCII, CJK, emoji. Returns True on success, False when Quartz is
    unavailable or the call raised (e.g. Input Monitoring TCC denied).
    """
    if not text:
        return True
    try:
        from Quartz.CoreGraphics import (
            CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap,
        )
        from Quartz import CGEventKeyboardSetUnicodeString
    except Exception:
        return False
    import time
    secs = delay_ms / 1000.0
    for ch in text:
        try:
            # keyCode 0 is a placeholder; CGEventKeyboardSetUnicodeString
            # overrides the interpreted glyph so the receiver gets the
            # right Unicode regardless of layout.
            down = CGEventCreateKeyboardEvent(None, 0, True)
            up = CGEventCreateKeyboardEvent(None, 0, False)
            buf = (ord(ch),)
            CGEventKeyboardSetUnicodeString(down, len(buf), buf)
            CGEventKeyboardSetUnicodeString(up, len(buf), buf)
            CGEventPost(kCGHIDEventTap, down)
            CGEventPost(kCGHIDEventTap, up)
            if secs:
                time.sleep(secs)
        except Exception:
            return False
    return True


def press_keycode(keycode: int, mod_flags: int = 0) -> bool:
    """Press a keycode with optional modifiers via CGEvent.

    Returns True on success, False when Quartz is unavailable or the call
    raised. Does NOT wait for the receiver to acknowledge — events dispatch
    into the system HID stream and the (current) foreground app picks them up.
    """
    try:
        from Quartz.CoreGraphics import (
            CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap,
        )
        from Quartz import CGEventSetFlags
    except Exception:
        return False
    try:
        down = CGEventCreateKeyboardEvent(None, keycode, True)
        up = CGEventCreateKeyboardEvent(None, keycode, False)
        if mod_flags:
            CGEventSetFlags(down, mod_flags)
            CGEventSetFlags(up, mod_flags)
        CGEventPost(kCGHIDEventTap, down)
        CGEventPost(kCGHIDEventTap, up)
        return True
    except Exception:
        return False


def press_key(key: str) -> bool:
    """Parse "Cmd+Q" → send via Quartz CGEvent.

    Returns True on success (event dispatched), False on parse/dispatch
    failure. Caller is responsible for verifying the receiver actually
    consumed the key — Quartz CG events that go to an unexpected foreground
    window look like no-ops.
    """
    code, mods = parse_key_combo(key)
    if code is None:
        return False
    return press_keycode(code, mods)
