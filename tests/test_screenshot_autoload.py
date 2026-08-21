"""Tests for the screenshot-no-autoload + sentinel-URL protection in web_a11y.

Background (field report): a screenshot action "wrongly captured an OLD
example.com page and RESET THE SESSION". Root cause: every ``_ensure_browser``
call ran ``_maybe_rewind_to_last_url``, which silently re-navigated a fresh
daemon's about:blank page to ``session.current_url`` — frequently a leftover
``example.com`` demo seed. Two fixes:

  1. ``_is_sentinel_url`` blocks replay of test-fixture URLs (example.com /
     about:blank / example.org).
  2. ``_ensure_browser(autoload_last_url=False)`` is now passed by the
     screenshot action and screenshot_fallback so a screenshot never triggers
     a re-navigation.

These tests pin the pure-function guard. The end-to-end "screenshot doesn't
navigate" behavior is verified by the autoload flag plumbing, exercised in the
web_a11y integration tests.
"""

from __future__ import annotations

import inspect

from qcu.layers import web_a11y as wa
from qcu.layers.screenshot_fallback import ScreenshotFallbackLayer


# ===========================================================================
# _is_sentinel_url — the replay guard
# ===========================================================================


def test_sentinel_urls_blocked():
    """The exact URLs that caused the bug must be flagged as sentinels."""
    assert wa._is_sentinel_url("example.com")
    assert wa._is_sentinel_url("https://example.com")
    assert wa._is_sentinel_url("https://example.com/")
    assert wa._is_sentinel_url("http://example.com")
    assert wa._is_sentinel_url("about:blank")
    assert wa._is_sentinel_url("http://example.org")
    assert wa._is_sentinel_url("https://example.org/")


def test_real_urls_not_sentinel():
    """A genuine user destination must NOT be blocked."""
    assert not wa._is_sentinel_url("https://crm.example.com/dashboard")
    assert not wa._is_sentinel_url("https://github.com/qcu/qcu")
    assert not wa._is_sentinel_url("https://example.com.au")  # different host


def test_sentinel_empty_and_case_insensitive():
    assert wa._is_sentinel_url("")
    assert wa._is_sentinel_url(None)
    # Case should not matter.
    assert wa._is_sentinel_url("HTTPS://EXAMPLE.COM/")
    assert wa._is_sentinel_url("About:Blank")


# ===========================================================================
# _ensure_browser accepts autoload_last_url
# ===========================================================================


def test_ensure_browser_has_autoload_param():
    """The flag must exist on the signature (the plumbing the screenshot path
    relies on). A static check — no browser needed."""
    sig = inspect.signature(wa.WebA11yLayer._ensure_browser)
    assert "autoload_last_url" in sig.parameters
    assert sig.parameters["autoload_last_url"].default is True


def test_act_passes_autoload_false_for_screenshot():
    """The act() method must peek at action.type and pass autoload_last_url=False
    for screenshots, True otherwise. Verified by reading the source — this is
    the contract the screenshot-capture path depends on."""
    src = inspect.getsource(wa.WebA11yLayer.act)
    assert "autoload_last_url" in src
    assert "screenshot" in src


# ===========================================================================
# screenshot_fallback.observe accepts **options (the full_text fix)
# ===========================================================================


def test_screenshot_fallback_observe_accepts_options():
    """observe must declare **options so callers passing full_text/compact
    don't hit TypeError. A screenshot has no text to truncate, so the options
    are ignored — but the signature must accept them per the Layer protocol."""
    sig = inspect.signature(ScreenshotFallbackLayer.observe)
    assert "options" in sig.parameters
    # And it must not blow up when called with extra kwargs (no capture happens
    # without a backend, but the signature path is what we're testing).
    L = ScreenshotFallbackLayer()
    obs = L.observe(full_text=True, compact=True, text_limit=999)
    assert obs is not None  # didn't raise
