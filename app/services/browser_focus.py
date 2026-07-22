"""Bring the automation browser window to the foreground (Windows-focused, portal-agnostic).

Why this exists: the Playwright browser is launched headed for login, Find Matches and Apply
runs, but on Windows it frequently opens behind the IDE/terminal or starts minimized, so the
user cannot see what the automation is doing.

Design rules:
  - Portal-agnostic: this module knows nothing about LinkedIn or Dice, so neither portal's logic
    depends on the other (it is shared infrastructure, like posting_time).
  - Best-effort ONLY: every failure is swallowed and logged at WARNING. Focus is a convenience,
    never a precondition -- login, search and apply must continue unchanged if it fails.
  - Headless is a hard no-op: there is no window to raise, and tests run headless.
  - Called only at major transition points (login opened, search begins, apply begins, next
    application form), never inside DOM interaction or polling loops -- repeatedly stealing
    focus would make the machine unusable.
  - No new dependencies: the Windows path uses the built-in user32 API through ctypes.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

# Win32 constants (winuser.h).
_SW_RESTORE = 9
_SW_SHOW = 5
# Chromium's top-level window class on Windows.
_CHROMIUM_WINDOW_CLASSES = ("Chrome_WidgetWin_1", "Chrome_WidgetWin_0")


def is_windows() -> bool:
    return sys.platform.startswith("win")


def focus_browser_window(page, headless: bool = False, reason: str = "") -> bool:
    """Bring the browser window showing `page` to the foreground.

    Returns True when a foreground attempt was made, False when it was skipped (headless) or
    could not be performed. NEVER raises: the caller's workflow must not depend on focus.
    """
    if headless:
        return False
    if page is None:
        return False

    raised = False
    # 1. Playwright's own cross-platform activation (also the only step on non-Windows).
    try:
        bring_to_front = getattr(page, "bring_to_front", None)
        if callable(bring_to_front):
            bring_to_front()
            raised = True
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("browser_focus bring_to_front failed%s: %s", _suffix(reason), exc)

    # 2. Windows only: restore a minimized window and force it in front of other apps.
    if is_windows():
        try:
            if _focus_native_window(_safe_title(page)):
                raised = True
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("browser_focus native window activation failed%s: %s", _suffix(reason), exc)
    return raised


def _suffix(reason: str) -> str:
    return f" ({reason})" if reason else ""


def _safe_title(page) -> str:
    try:
        return str(page.title() or "")
    except Exception:
        return ""


def _focus_native_window(page_title: str) -> bool:
    """Restore + foreground the Chromium window on Windows via user32. Returns True on success.

    The window is matched by Chromium's window class; when the page title is known it is used to
    pick the RIGHT Chromium window, so a user's own Chrome window is not hijacked.
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    handles: list[int] = []

    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _collect(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        class_buffer = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buffer, 256)
        if class_buffer.value in _CHROMIUM_WINDOW_CLASSES:
            handles.append(hwnd)
        return True

    user32.EnumWindows(enum_proc(_collect), 0)
    if not handles:
        return False

    target = _best_handle(user32, handles, page_title)
    if not target:
        return False

    if user32.IsIconic(target):
        user32.ShowWindow(target, _SW_RESTORE)
    else:
        user32.ShowWindow(target, _SW_SHOW)
    if user32.SetForegroundWindow(target):
        return True
    # SetForegroundWindow is refused when the calling process does not own the foreground;
    # SwitchToThisWindow is the documented, dependency-free fallback.
    try:
        user32.SwitchToThisWindow(target, True)
        return True
    except Exception:
        return False


def _best_handle(user32, handles: list[int], page_title: str) -> int | None:
    """Prefer the Chromium window whose caption contains the page title; else the first one."""
    import ctypes

    if page_title:
        needle = page_title.strip().lower()
        for hwnd in handles:
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                continue
            buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if needle and needle in buffer.value.lower():
                return hwnd
    return handles[0] if handles else None
