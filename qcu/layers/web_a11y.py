"""Web a11y layer — Playwright + CDP-driven accessibility tree.

The default control plane. Strategy:

1. Lazy-launch a single Chromium process via Playwright Python.
2. Open a CDP session per page: ``Accessibility.enable`` then
   ``Accessibility.getFullAXTree`` gives us a structured tree with stable
   ``backendDOMNodeId`` handles.
3. For each interactive node we resolve ``DOM.getBoxModel`` for a click
   target and tag the DOM element with ``data-llm-ref="ref_N"`` so future
   actions can be done via a CSS selector (cheaper + more robust than
   coordinates across re-layouts).
4. ``act(ref=...)`` prefers ``page.locator('[data-llm-ref="ref_N"]').click()``;
   if the locator is gone (page mutated) we fall back to the cached
   coordinate.
5. Screenshots are never taken unless the router asks for one.

Reference: chromedevtools.github.io/devtools-protocol/tot/Accessibility/
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

from qcu.common.normalize import clean_text, from_aria, INTERACTIVE_ROLES, wait_ms as _wait_ms
from qcu.common.types import (
    Action,
    Element,
    LayerResult,
    Observation,
    Rect,
)
from qcu.layers.base import Layer
from qcu.layers.runtime import register


async def _goto_with_retry(page: Any, url: str, *, attempts: int = 3) -> None:
    """Navigate with retry on Chromium connection-drop errors.

    We've seen ``ERR_CONNECTION_CLOSED`` and ``ERR_NETWORK_CHANGED`` on
    flaky external links (cellular, captive portals, slow TLS handshakes).
    These are transient; a quick retry usually succeeds.
    """
    import asyncio

    last_err: Optional[BaseException] = None
    for i in range(attempts):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            return
        except Exception as e:  # noqa: BLE001
            msg = str(e).lower()
            transient = (
                "err_connection_closed" in msg
                or "err_network_changed" in msg
                or "err_aborted" in msg
                or "err_internet_disconnected" in msg
                or "err_timed_out" in msg
                or "err_empty_response" in msg
            )
            last_err = e
            if not transient or i == attempts - 1:
                raise
            await asyncio.sleep(0.5 * (i + 1))
    if last_err is not None:
        raise last_err


def _is_mac() -> bool:
    """Best-effort runtime check for macOS so the fill fallback uses Meta
    (⌘) instead of Control for Select-All."""
    import platform

    return platform.system() == "Darwin"


# URLs that must NEVER be auto-replayed onto a fresh daemon's about:blank page.
# These are test fixtures and protocol sentinels, not real user destinations.
# Replaying ``example.com`` (the pervasive demo seed) is the root cause of the
# "screenshot grabbed an old page" field report — see _maybe_rewind_to_last_url.
_SENTINEL_URLS = frozenset({
    "about:blank",
    "example.com",
    "http://example.com",
    "https://example.com",
    "https://example.com/",
    "http://example.org",
    "https://example.org",
})


def _is_sentinel_url(url: str) -> bool:
    """True iff ``url`` is a test/protocol sentinel that must not be replayed."""
    if not url:
        return True
    return url.strip().rstrip("/").lower() in {
        u.rstrip("/").lower() for u in _SENTINEL_URLS
    }


def _ax_property(node: dict[str, Any], name: str, default: Any = None) -> Any:
    """Return an AX property value without mistaking missing state for truth."""
    for prop in node.get("properties") or []:
        if prop.get("name") == name:
            value = prop.get("value", default)
            return value.get("value", default) if isinstance(value, dict) else value
    return default


def _valid_coordinate(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _locator_failure(exc: BaseException) -> str:
    """Classify common Playwright locator failures for actionable errors."""
    message = str(exc).lower()
    if "strict mode violation" in message or "resolved to" in message and "elements" in message:
        return "ambiguous"
    if "timeout" in message:
        return "timeout"
    if "not visible" in message or "outside of the viewport" in message:
        return "not_visible"
    if "intercepts pointer" in message or "not enabled" in message or "not editable" in message:
        return "not_actionable"
    if "detached" in message or "not attached" in message:
        return "detached"
    return "locator_error"


def _is_same_origin(base: str, href: str) -> bool:
    try:
        a, b = urlparse(base), urlparse(href)
        return (a.scheme.lower(), a.hostname, a.port) == (b.scheme.lower(), b.hostname, b.port)
    except Exception:
        return False


@register("web_a11y")
class WebA11yLayer(Layer):
    name = "web_a11y"

    def __init__(self) -> None:
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cdp: Any = None
        self._last_refs: dict[str, dict[str, Any]] = {}  # ref -> {backend_id, cx, cy, bounds}
        self._closed = False
        # Whether THIS process launched the browser and therefore owns its
        # lifecycle. The daemon is always detached, so this is effectively
        # always False — but it documents intent and guards close() against
        # ever terminating a shared browser.
        self._owns_browser = False
        self._cdp_port: Optional[int] = None  # port of the daemon we connected to
        # Restore cached refs from session (if any) so the first `qcu act`
        # after a fresh process can still click by ref.
        try:
            from qcu.session import load as _load

            s = _load()
            if s and getattr(s, "last_refs", None):
                for entry in getattr(s, "last_refs", None) or []:
                    ref = entry.get("ref")
                    if not ref:
                        continue
                    b = entry.get("bounds")
                    bounds = None
                    if b:
                        bounds = Rect(b["x"], b["y"], b["width"], b["height"])
                    self._last_refs[ref] = {
                        "backend_id": entry.get("backend_id"),
                        "cx": entry.get("cx"),
                        "cy": entry.get("cy"),
                        "role": entry.get("role"),
                        "name": entry.get("name"),
                        "bounds": bounds,
                    }
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_browser(self, *, autoload_last_url: bool = True) -> None:
        """Attach to the long-lived Chromium daemon (launching it if needed).

        Each ``qcu`` CLI call is a fresh process. To keep page focus and
        unsubmitted form state alive across commands, we NEVER launch a browser
        per process. Instead:

        1. Look up the daemon's CDP port in the session file.
        2. If the endpoint responds → ``connect_over_cdp`` (cheap client).
        3. If not → lazily launch a detached Chromium daemon, wait for its CDP
           endpoint, then connect. The daemon outlives this process.

        ``connect_over_cdp`` attaches to the existing browser without resetting
        its DOM, so unsubmitted inputs, focus, and scroll position survive.

        ``autoload_last_url``: when True (default), a fresh daemon whose page is
        still on ``about:blank`` will be silently re-navigated to the session's
        last ``current_url``. Set to **False for screenshot paths** — a
        screenshot must capture the page AS-IS; silently re-navigating to a
        stale ``current_url`` (e.g. a leftover ``example.com``) before capturing
        is exactly the "screenshot grabbed an old page" bug.
        """
        if self._page is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as e:
            raise RuntimeError(
                "playwright is required for web_a11y layer; "
                "install with `pip install playwright && python -m playwright install chromium`"
            ) from e

        from qcu.session import _qcu_home, load as load_session, new, patch
        from qcu.layers import browser_daemon as bd

        # Resolve the user-data-dir (must be explicit: Chrome 136+ ignores
        # --remote-debugging-port for the default profile).
        ud = os.environ.get("QCU_USER_DATA_DIR")
        profile_owned = False
        if not ud:
            ud_path = _qcu_home() / "browser-data"
            ud_path.mkdir(parents=True, exist_ok=True)
            ud = str(ud_path)
            profile_owned = True

        # Headed mode: read from the persisted session first (this is what
        # makes `QCU_HEADLESS=0 qcu session start` survive across processes).
        # Fall back to QCU_HEADLESS only when no session has been created yet,
        # so manual daemon launches still work.
        s = load_session()
        if s is not None and getattr(s, "headless", None) is not None:
            headless = bool(s.headless)
        else:
            headless = os.environ.get("QCU_HEADLESS", "1") != "0"
        channel = os.environ.get("QCU_BROWSER_CHANNEL")  # "chrome" / "msedge" / None

        # Decide the port: reuse a live daemon if one is recorded and responding.
        port = None
        if s is not None and s.browser_debug_port and bd.is_cdp_alive(s.browser_debug_port):
            # Reject a live daemon whose recorded headed mode conflicts with the
            # persisted request — Chromium can't switch modes in place.
            same_mode = getattr(s, "headless", headless) == headless
            if same_mode:
                port = s.browser_debug_port
            else:
                # Stop the wrong-mode daemon so a correct one launches next.
                if s.browser_pid:
                    with contextlib.suppress(Exception):
                        bd.kill_pid(s.browser_pid)
                from qcu.session import patch as _patch

                _patch(browser_pid=None, browser_debug_port=None, browser_status="stopped", headless=headless)

        if port is None:
            # Lazily launch (or relaunch after a crash) the detached daemon.
            recorded_pid = s.browser_pid if s is not None else None
            pid, port = bd.ensure_daemon(
                user_data_dir=ud,
                port=None,
                browser_pid=recorded_pid,
                headless=headless,
                channel=channel,
            )
            # Persist so the next CLI process can reuse without relaunching.
            # patch() is a no-op when no session exists yet, so create one
            # first — _ensure_browser is the authority on daemon state and
            # must guarantee it's recorded regardless of who called us.
            if s is None:
                s = new(
                    context="web",
                    layer="web_a11y",
                    user_data_dir=ud,
                    profile_owned=profile_owned,
                    browser_channel=channel,
                    headless=headless,
                    browser_debug_port=port,
                    browser_pid=pid or None,
                )
            else:
                patch(
                    browser_debug_port=port,
                    browser_pid=pid or None,
                    user_data_dir=ud,
                    profile_owned=profile_owned,
                    browser_channel=channel,
                    headless=headless,
                )
        self._cdp_port = port
        # The daemon is detached and shared; this process never owns it.
        self._owns_browser = False

        self._loop = asyncio.new_event_loop()

        async def _connect() -> None:
            pw = await async_playwright().start()
            self._playwright = pw
            # Attach to the running daemon — does NOT spawn a new browser and
            # does NOT reset the existing pages' DOM/focus/form state.
            self._browser = await pw.chromium.connect_over_cdp(bd.cdp_url(port))
            # contexts[0] is the default (--user-data-dir) context. It already
            # holds any live tabs from prior commands.
            contexts = self._browser.contexts
            self._context = contexts[0] if contexts else await self._browser.new_context()
            if self._context.pages:
                self._page = self._context.pages[0]
            else:
                self._page = await self._context.new_page()
            cdp = await self._context.new_cdp_session(self._page)
            await cdp.send("Accessibility.enable")
            await cdp.send("DOM.enable")
            self._cdp = cdp

        self._loop.run_until_complete(_connect())

        # If the page is still on about:blank and the session remembers a URL,
        # rewind so a brand-new daemon shows the user's last location. On a
        # reused daemon this is a no-op (the live page isn't about:blank).
        # Skipped for screenshot paths (autoload_last_url=False): a screenshot
        # must capture the current page, not silently jump to a stale URL.
        if autoload_last_url:
            self._maybe_rewind_to_last_url()

    def _maybe_rewind_to_last_url(self) -> None:
        """If the persisted page is on about:blank and session has current_url, replay it.

        Guarded against the "screenshot grabbed an OLD example.com page" bug:
        the demo seed ``example.com`` and other sentinel URLs are never replayed
        — they are test fixtures, not real user destinations, and replaying them
        onto a fresh daemon is what made every screenshot after a daemon restart
        silently re-navigate to a stale page. Real URLs are still replayed (that
        is the intended convenience for a brand-new daemon), but a navigation
        failure now surfaces on stderr instead of being swallowed.
        """
        if self._page is None or self._loop is None:
            return
        from qcu.session import load as _load

        s = _load()
        if s is None or not s.current_url:
            return
        if self._page.url not in ("", "about:blank"):
            return
        # Never replay sentinel/demo URLs — they are test fixtures, and
        # replaying example.com onto a fresh daemon is the root cause of the
        # "screenshot grabbed an old page" report.
        target = s.current_url
        if _is_sentinel_url(target):
            import sys as _sys
            print(
                f"[qcu] session.current_url is a sentinel ({target!r}); not "
                f"auto-replaying onto the about:blank daemon page. Navigate "
                f"explicitly if you want to load a page.",
                file=_sys.stderr,
            )
            return

        async def _go() -> None:
            assert self._page is not None
            try:
                await _goto_with_retry(self._page, target, attempts=2)
            except Exception as e:
                import sys as _sys
                print(
                    f"[qcu] failed to auto-reload last URL {target!r}: "
                    f"{type(e).__name__}: {e}. Page stays on about:blank.",
                    file=_sys.stderr,
                )

        try:
            self._loop.run_until_complete(_go())
        except Exception as e:
            import sys as _sys
            print(
                f"[qcu] auto-reload of {target!r} raised: "
                f"{type(e).__name__}: {e}. Page stays on about:blank.",
                file=_sys.stderr,
            )

    def _navigate_async(self, url: str) -> None:
        if self._page is None or self._loop is None:
            return

        async def _go() -> None:
            assert self._page is not None
            try:
                await _goto_with_retry(self._page, url, attempts=2)
            except Exception:
                pass

        try:
            self._loop.run_until_complete(_go())
        except Exception:
            pass

    def close(self) -> None:
        """Drop this process's CDP connection WITHOUT killing the daemon.

        Critical: on a ``connect_over_cdp`` browser, calling
        ``context.close()`` or ``browser.close()`` terminates the shared daemon
        and wipes every other client's page state. We must only stop the local
        Playwright driver, which closes the websocket client while leaving the
        detached Chromium process running. ``qcu session end`` is the one place
        that actually stops the daemon (via ``browser_daemon.kill_pid``).
        """
        if self._closed:
            return
        self._closed = True
        if self._loop is None:
            return
        try:

            async def _stop() -> None:
                # Deliberately do NOT close context/browser here — that would
                # kill the shared daemon. Only tear down the local driver.
                if self._playwright is not None:
                    try:
                        await self._playwright.stop()
                    except Exception:
                        pass

            self._loop.run_until_complete(_stop())
        finally:
            try:
                self._loop.close()
            except Exception:
                pass
            self._loop = None
            # Drop handles, but keep _last_refs intact — it is rebuilt from the
            # session file on the next process's __init__, and the live DOM's
            # data-llm-ref attributes survive because we didn't close anything.
            self._page = None
            self._browser = None
            self._context = None
            self._cdp = None

    # ------------------------------------------------------------------
    # Observe
    # ------------------------------------------------------------------

    def observe(self, max_depth: int = 10, **options: Any) -> Observation:
        """Observe the page while preserving the legacy positional depth API.

        Optional controls include ``text_limit``, ``total_limit``, ``roles``,
        ``query``, ``offset``, ``limit``, ``full_text``, ``tail`` and ``compact``.
        """
        self._ensure_browser()
        assert self._loop is not None
        return self._loop.run_until_complete(self._observe_async(max_depth, **options))

    async def _observe_async(self, max_depth: int, **options: Any) -> Observation:
        page = self._page
        cdp = self._cdp
        assert page is not None and cdp is not None

        # Wait for the page to be ready before snapshotting. This replaces the
        # old pattern of a manual fixed `wait` after every navigate/click:
        # domcontentloaded is near-instant, and a short networkidle cap catches
        # the last in-flight XHRs so the a11y tree reflects the final DOM
        # (otherwise infobox data injected by JS can be missed). Both have
        # tight timeouts so a stuck page can't hang observe.
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        try:
            await page.wait_for_load_state("networkidle", timeout=1500)
        except Exception:
            pass

        requested_depth = max_depth
        effective_depth = max_depth if max_depth > 0 else -1
        text_limit = max(0, int(options.get("text_limit", 80)))
        total_limit_opt = options.get("total_limit")
        total_limit = max(0, int(total_limit_opt)) if total_limit_opt is not None else None
        full_text = bool(options.get("full_text", False))
        compact = bool(options.get("compact", False))
        tail = bool(options.get("tail", False))
        offset = max(0, int(options.get("offset", 0)))
        page_limit_opt = options.get("limit")
        page_limit = max(0, int(page_limit_opt)) if page_limit_opt is not None else None
        query = clean_text(options.get("query"), max_len=500).casefold()
        requested_roles = options.get("roles") or options.get("role")
        if isinstance(requested_roles, str):
            role_filter = {part.strip() for part in requested_roles.split(",") if part.strip()}
        else:
            role_filter = {str(part) for part in requested_roles or []}

        # Observe with adaptive deepening: a shallow request that returns a
        # large tree but almost no interactive refs almost certainly means the
        # interesting controls live below the requested depth (HN's front page
        # is the canonical example: depth 6 surfaces only footers, depth 14
        # surfaces all story/comment links). Re-run once with a deeper budget so
        # we don't silently hide the page's main content.
        adaptive_used = False
        cdp_depth = effective_depth
        tree_res = await cdp.send("Accessibility.getFullAXTree", {"depth": cdp_depth})
        nodes = tree_res.get("nodes", []) or []

        def _interactive_count(ns: list[dict[str, Any]]) -> int:
            n = 0
            for nd in ns:
                role_obj = nd.get("role") or {}
                role_raw = role_obj.get("value") if isinstance(role_obj, dict) else None
                if not nd.get("ignored") and from_aria(role_raw) in INTERACTIVE_ROLES:
                    n += 1
            return n

        if (
            0 < requested_depth <= 8
            and len(nodes) >= 60
            and _interactive_count(nodes) <= 5
        ):
            cdp_depth = max(requested_depth, 14)
            tree_res = await cdp.send("Accessibility.getFullAXTree", {"depth": cdp_depth})
            nodes = tree_res.get("nodes", []) or []
            adaptive_used = True

        by_id = {n.get("nodeId"): n for n in nodes if n.get("nodeId")}
        # children-of map for parent lookup so we can carry structural context
        # (parent_id / ancestor_refs) on every emitted node — this is what lets
        # us keep a HN comment body attached to its reply link after collapsing
        # the tree to a flat text dump.
        parent_of: dict[Any, Any] = {}
        for n in nodes:
            for cid in n.get("childIds") or []:
                parent_of[cid] = n.get("nodeId")

        roots = [n for n in nodes if not n.get("parentId")]

        url = page.url
        title = await page.title()

        elements: list[Element] = []
        # Ordered (role, line) tuples in true AX traversal order. Unlike the old
        # structured_lines+text_lines split, this preserves DOM adjacency so a
        # username, a reply link, and the comment body that follow each other in
        # the page stay together in the raw_tree output.
        ordered: list[tuple[str, str, Optional[str]]] = []  # (role, line, node_id)
        new_refs: dict[str, dict[str, Any]] = {}
        seen_backend: dict[int, str] = {}
        counter = 0
        deepest_seen = 0
        text_kept = 0
        text_dropped = 0
        structured_kept = 0
        structured_dropped = 0
        truncated_elements: list[str] = []
        max_text_len_seen = 0

        # Read the base URL once for same-origin/target resolution on links.
        base_url = url

        # Generation for refs so a stale `ref_N` from an older observation can
        # no longer be silently replayed against a fresh DOM. We advance the
        # session's observation_generation as part of persisting refs below.
        generation = 0
        try:
            from qcu.session import load as _load

            _session = _load()
            if _session is not None:
                generation = int(getattr(_session, "observation_generation", 0) or 0) + 1
        except Exception:
            generation = 0
        gen_prefix = f"obs_{generation}" if generation else ""

        # Roles whose text is worth showing the LLM even though they aren't
        # actable. This is what makes infobox dates / form labels / headings
        # readable from raw_tree WITHOUT needing a screenshot.
        # Split: STRUCTURED roles are always kept (high signal); TEXT roles are
        # capped (bulk paragraph content).
        _STRUCTURED_ROLES = frozenset(
            {
                "cell",
                "row",
                "columnheader",
                "rowheader",
                "heading",
                "list_item",
                "option",
                "menu_item",
                "tab_item",
                "tooltip",
                "alert",
                "dialog",
            }
        )
        # Low-signal bulk text — collected but capped by text_limit so it
        # can't crowd out the structured signal above.
        _TEXT_ROLES = frozenset({"text", "group", "link"})

        node_meta: dict[Any, dict[str, Any]] = {}

        def walk(node: dict[str, Any], depth: int) -> None:
            nonlocal counter, text_kept, text_dropped, structured_kept, structured_dropped
            nonlocal deepest_seen, max_text_len_seen
            deepest_seen = max(deepest_seen, depth)
            node_id = node.get("nodeId")
            role_obj = node.get("role") or {}
            role_raw = role_obj.get("value") if isinstance(role_obj, dict) else None
            # Decide name length cap by role. Interactive labels stay tight so
            # the token budget isn't blown; informational text containers (text,
            # cell, row, heading) carry the actual page content (HN comments,
            # infobox values, paragraphs) and must be readable in full. This is
            # what fixed the bug where a Hacker News comment was truncated to
            # 200 chars.
            _TEXTY = {"text", "cell", "row", "columnheader", "rowheader", "heading"}
            norm_preview = from_aria(role_raw)
            name_cap = 1000 if norm_preview in _TEXTY else 200
            name_obj = node.get("name") or {}
            name_raw_full = clean_text(
                name_obj.get("value") if isinstance(name_obj, dict) else None,
                max_len=name_cap,
            )
            if norm_preview in _TEXTY and name_raw_full:
                # Track the raw length before the cap so callers can tell they
                # received a truncated value.
                raw_len = len(
                    (name_obj.get("value") if isinstance(name_obj, dict) else "") or ""
                )
                max_text_len_seen = max(max_text_len_seen, raw_len)
                if len(clean_text(name_obj.get("value") if isinstance(name_obj, dict) else None, max_len=10**9)) > name_cap:
                    truncated_elements.append(name_raw_full[-40:])
            value_obj = node.get("value") or {}
            value_raw = clean_text(
                value_obj.get("value") if isinstance(value_obj, dict) else None, max_len=1000
            )
            ignored = bool(node.get("ignored"))

            pid = parent_of.get(node_id)
            ancestor_refs: list[str] = []

            def _line_for_role(role_norm: str, nm: Optional[str], val: Optional[str]) -> str:
                indent = "  " * depth
                line = f"{indent}{role_norm}"
                if nm:
                    line += f" {nm!r}"
                if val:
                    line += f" = {val!r}"
                return line

            if not ignored or name_raw_full:
                role_norm = from_aria(role_raw)
                is_interactive = role_norm in INTERACTIVE_ROLES
                # Role/name filtering (qcu find / --role / --name). Filtering is
                # applied here in the layer (not after caching) so the returned
                # elements and self._last_refs always agree.
                passes_filter = True
                if role_filter and role_norm not in role_filter:
                    passes_filter = False
                if query and (not name_raw_full or query not in name_raw_full.casefold()):
                    passes_filter = False

                if is_interactive:
                    bid = node.get("backendDOMNodeId")
                    if bid is not None and bid in seen_backend:
                        ref = seen_backend[bid]
                    else:
                        ref = f"{gen_prefix}:ref_{counter}" if gen_prefix else f"ref_{counter}"
                        counter += 1
                        if bid is not None:
                            seen_backend[bid] = ref

                    # Truthful enabled/focused from AX properties instead of the
                    # hardcoded enabled=True/focused=False that made disabled
                    # buttons look clickable.
                    disabled = _ax_property(node, "disabled", False)
                    focused = _ax_property(node, "focused", False)
                    enabled = not bool(disabled)

                    props: dict[str, Any] = {
                        "raw_role": role_raw,
                        "depth": depth,
                    }
                    # Link target metadata: this is what lets the LLM tell apart
                    # "X comments" (same-origin internal page) from an article
                    # title link (external site) without site-specific knowledge.
                    if role_norm == "link":
                        href = _ax_property(node, "url")
                        props["raw_role"] = role_raw
                        if href:
                            props["href"] = href
                            try:
                                abs_href = urljoin(base_url, href)
                                props["absolute_href"] = abs_href
                                props["same_origin"] = _is_same_origin(base_url, abs_href)
                            except Exception:
                                props["same_origin"] = _is_same_origin(base_url, href)
                        for extra_prop in ("target", "rel", "download"):
                            v = _ax_property(node, extra_prop)
                            if v not in (None, False):
                                props[extra_prop] = v
                        if "target" not in props:
                            # AX doesn't usually expose target; expose a hint
                            # derived from href where possible.
                            pass
                    # Extra state flags useful for forms.
                    for st in ("checked", "selected", "expanded", "pressed", "readonly", "required"):
                        v = _ax_property(node, st, None)
                        if v not in (None, False):
                            props[st] = v

                    el = Element(
                        ref=ref,
                        role=role_norm,
                        name=name_raw_full or None,
                        value=value_raw or None,
                        enabled=enabled,
                        focused=bool(focused),
                        bounds=None,  # filled in by the batched query below
                        backend_id=str(bid) if bid is not None else None,
                        properties=props,
                    )
                    if ancestor_count[0] and pid is not None:
                        el.properties.setdefault("parent_id", str(pid))
                    elements.append(el)
                    new_refs[ref] = {
                        "backend_id": bid,
                        "node_id": node_id,
                        "parent_id": pid,
                        "cx": None,
                        "cy": None,
                        "bounds": None,
                        "name": name_raw_full,
                        "role": role_norm,
                    }
                    node_meta[node_id] = {"ref": ref, "role": role_norm, "depth": depth, "parent_id": pid}
                    if passes_filter:
                        ordered.append((role_norm, f"{_line_for_role(role_norm, name_raw_full, value_raw)} [{ref}]", str(node_id) if node_id is not None else None))
                elif name_raw_full and role_norm in _STRUCTURED_ROLES:
                    # High-signal structured text (infobox cells/rows, headings,
                    # list items, ...). Always kept unless a total node budget
                    # forces trimming — this is what lets the LLM read
                    # "Born / 23 June 1912" without a screenshot.
                    if total_limit is None or (structured_kept + text_kept) < total_limit:
                        ordered.append((role_norm, _line_for_role(role_norm, name_raw_full, value_raw), str(node_id) if node_id is not None else None))
                        structured_kept += 1
                        node_meta[node_id] = {"role": role_norm, "depth": depth, "parent_id": pid}
                    else:
                        structured_dropped += 1
                elif name_raw_full and role_norm in _TEXT_ROLES:
                    # Bulk text — capped so it can't crowd out the structured
                    # signal above. full_text lifts the cap entirely (e.g. to
                    # read an entire long comment verbatim).
                    effective_text_limit = 10**9 if full_text else text_limit
                    if full_text or text_kept < effective_text_limit:
                        ordered.append((role_norm, _line_for_role(role_norm, name_raw_full, value_raw), str(node_id) if node_id is not None else None))
                        text_kept += 1
                        node_meta[node_id] = {"role": role_norm, "depth": depth, "parent_id": pid}
                    else:
                        text_dropped += 1
            for child_id in node.get("childIds") or []:
                child = by_id.get(child_id)
                if child is not None:
                    walk(child, depth + 1)

        ancestor_count = [0]
        for r in roots:
            walk(r, 0)

        # ---- Concurrent batched geometry -----------------------------------
        # Each interactive ref needs (a) its data-llm-ref attribute injected so
        # subsequent locator clicks survive, and (b) a bounding rect for the
        # coordinate-fallback path. Both go through one Runtime.callFunctionOn
        # per node that uses `this` (the resolved node), not a function
        # parameter — the old `function(el){...}` form set the attribute on the
        # *ref string*, which silently broke every click fallback. Issuing the
        # calls via gather overlaps the round-trips instead of paying N×RTT
        # serially (the old ~360ms-on-HN, much worse on big pages).
        refs_with_backend = [
            (ref, info["backend_id"])
            for ref, info in new_refs.items()
            if info.get("backend_id") is not None
        ]
        geom_failed = 0

        async def _geom_one(ref: str, bid: Any) -> None:
            nonlocal geom_failed
            try:
                remote = (await cdp.send("DOM.resolveNode", {"backendNodeId": bid}))["object"]
                rect = await cdp.send(
                    "Runtime.callFunctionOn",
                    {
                        "objectId": remote["objectId"],
                        "functionDeclaration": (
                            "function(ref){"
                            "  this.setAttribute('data-llm-ref', ref);"
                            "  const r = this.getBoundingClientRect();"
                            "  return (r.width > 0 && r.height > 0)"
                            "    ? {x: r.left, y: r.top, width: r.width, height: r.height} : null;"
                            "}"
                        ),
                        "arguments": [{"value": ref}],
                        "returnByValue": True,
                    },
                )
                try:
                    await cdp.send("Runtime.releaseObject", {"objectId": remote["objectId"]})
                except Exception:
                    pass
                val = rect.get("result", {}).get("value") if isinstance(rect, dict) else None
                if val:
                    bounds = Rect(val["x"], val["y"], val["width"], val["height"])
                    info = new_refs[ref]
                    info["bounds"] = bounds
                    info["cx"] = bounds.cx
                    info["cy"] = bounds.cy
                    # O(1) ref→element lookup instead of the previous O(N) scan
                    # that scaled quadratically with the interactive count.
                    el = element_index.get(ref)
                    if el is not None:
                        el.bounds = bounds
                else:
                    geom_failed += 1
            except Exception:
                geom_failed += 1

        element_index = {e.ref: e for e in elements}
        if refs_with_backend:
            await asyncio.gather(*(_geom_one(ref, bid) for ref, bid in refs_with_backend))

        self._last_refs = new_refs

        # Persist refs and the observation generation atomically so a fresh CLI
        # invocation can click by ref without re-observing first, and so a stale
        # `ref_N` from an earlier observation can be rejected.
        try:
            from qcu.session import record_observation

            serialized = []
            for ref, info in new_refs.items():
                b = info.get("bounds")
                serialized.append(
                    {
                        "ref": ref,
                        "backend_id": info.get("backend_id"),
                        "cx": info.get("cx"),
                        "cy": info.get("cy"),
                        "role": info.get("role"),
                        "name": info.get("name"),
                        "bounds": (
                            {"x": b.x, "y": b.y, "width": b.width, "height": b.height}
                            if b is not None
                            else None
                        ),
                    }
                )
            # Keep current_url in sync with the live page, advance the
            # observation generation, and persist the refs in one transaction.
            # Skip data:/blob: URLs which aren't replayable.
            replay_url = url if url and not url.startswith(("data:", "blob:")) else None
            record_observation(
                {
                    "context": "web",
                    "url_or_app": url,
                    "title": title,
                    "elements": [e.ref for e in elements],
                    "n_interactive": len(elements),
                    "requested_depth": requested_depth,
                    "deepest_depth": deepest_seen,
                    "adaptive_used": adaptive_used,
                },
                current_url=replay_url,
                refs=serialized,
            )
        except Exception:
            pass

        # Role/name/offset/limit pagination applied AFTER elements are collected
        # but BEFORE building raw_tree — the returned element list and the
        # raw_tree lines stay consistent.
        filtered = elements
        if role_filter or query:
            filtered = [
                e for e in elements
                if (not role_filter or e.role in role_filter)
                and (not query or query in (e.name or "").casefold())
            ]
        page_start = offset
        page_end = None if page_limit is None else page_start + page_limit
        filtered = filtered[page_start:page_end]

        # Build ordered raw_tree in true traversal order so comments/replies
        # keep their DOM adjacency. The walk already ordered nodes; tail keeps
        # only the last text_limit text-role lines for "read the last comment".
        lines: list[str] = []
        if not compact:
            text_lines_buf: list[str] = []
            for role_norm, line, _ in ordered:
                if role_norm in _TEXT_ROLES:
                    text_lines_buf.append(line)
                else:
                    lines.append(line)
            if tail:
                text_lines_buf = text_lines_buf[-text_limit:] if text_limit else text_lines_buf
            lines.extend(text_lines_buf)

        # Truncation diagnostics: every place we silently dropped content now
        # reports it so the LLM knows the tree is partial.
        total_kept = structured_kept + text_kept + len(elements)
        total_dropped = structured_dropped + text_dropped
        tree_truncated = bool(
            total_dropped
            or geom_failed
            or truncated_elements
            or (page_limit is not None and len(elements) > (page_start + (page_limit or 0)))
            or adaptive_used
        )

        routing_meta = {
            "layer": self.name,
            "n_refs": len(elements),
            "n_returned": len(filtered),
            "n_interactive": len(elements),
            "n_structured_lines": structured_kept,
            "n_text_lines": text_kept,
            "text_dropped": text_dropped,
            "structured_dropped": structured_dropped,
            "geom_failed": geom_failed,
            "tree_truncated": tree_truncated,
            "truncated_elements": truncated_elements,
            "max_text_seen": max_text_len_seen,
            "requested_depth": requested_depth,
            "deepest_returned_depth": deepest_seen,
            "adaptive_used": adaptive_used,
            "options": {
                "text_limit": text_limit,
                "total_limit": total_limit,
                "full_text": full_text,
                "compact": compact,
                "tail": tail,
                "offset": offset,
                "limit": page_limit,
                "roles": sorted(role_filter) if role_filter else None,
                "query": query or None,
            },
        }

        return Observation(
            context="web",
            url_or_app=url,
            title=title,
            elements=filtered,
            routing_meta=routing_meta,
            raw_tree=("\n".join(lines) if lines else None),
        )

    # ------------------------------------------------------------------
    # Act
    # ------------------------------------------------------------------

    def act(self, action: Action) -> LayerResult:
        # A screenshot must capture the page AS-IS — never silently re-navigate
        # to a stale session.current_url first. Pass autoload_last_url=False so
        # _ensure_browser skips _maybe_rewind_to_last_url for this call.
        autoload = action.type != "screenshot"
        self._ensure_browser(autoload_last_url=autoload)
        assert self._loop is not None
        try:
            return self._loop.run_until_complete(self._act_async(action))
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"{type(e).__name__}: {e}")

    async def _act_async(self, action: Action) -> LayerResult:
        page = self._page
        assert page is not None
        atype = action.type
        params = action.params or {}

        if atype == "navigate":
            url = params.get("url") or params.get("value")
            if not url:
                return LayerResult(ok=False, layer=self.name, message="navigate needs params.url")
            try:
                await _goto_with_retry(page, url, attempts=3)
            except Exception as e:  # noqa: BLE001
                return LayerResult(
                    ok=False, layer=self.name, message=f"navigate failed: {type(e).__name__}: {e}"
                )
            # Persist the URL so a fresh `qcu` invocation can rewind here, and
            # immediately drop any stale refs — they point at the previous page.
            try:
                from qcu.session import invalidate_observation

                invalidate_observation(current_url=url, document_changed=True)
            except Exception:
                pass
            return LayerResult(ok=True, layer=self.name, message=f"navigated to {url}")

        if atype in ("go_back", "go_forward"):
            # Verify the navigation actually happened. We compare BEFORE and
            # AFTER url and history length on *every* path — including the
            # non-exception path — so `page.go_back()` returning `None` on a
            # page with no history can no longer be reported as a successful
            # navigation. bfcache + same-document (SPA hash/pushState) history
            # moves legitimately keep the URL identical, so a history-length or
            # document-generation change also counts as success.
            before = page.url
            try:
                before_history = await page.evaluate("() => history.length")
            except Exception:
                before_history = None
            before_gen = await self._document_generation()
            try:
                if atype == "go_back":
                    await page.go_back(timeout=5000)
                else:
                    await page.go_forward(timeout=5000)
                dispatch_ok = True
                err: Optional[BaseException] = None
            except Exception as e:  # noqa: BLE001
                # bfcache restores don't fire domcontentloaded, so Playwright's
                # wait_until often times out even though the URL already moved.
                dispatch_ok = False
                err = e

            await asyncio.sleep(0.05)
            after = page.url
            try:
                after_history = await page.evaluate("() => history.length")
            except Exception:
                after_history = None
            after_gen = await self._document_generation()
            moved = (
                after != before
                or (after_history is not None and before_history is not None and after_history != before_history)
                or (after_gen != before_gen)
            )
            try:
                from qcu.session import invalidate_observation

                invalidate_observation(current_url=after, document_changed=moved)
            except Exception:
                pass
            if not moved:
                msg = "no history to move" if not dispatch_ok else (f"{type(err).__name__}: {err}" if err else "no_effect")
                return LayerResult(
                    ok=False,
                    layer=self.name,
                    message=f"{atype}: {msg} (still at {after})",
                    data={
                        "dispatched": dispatch_ok,
                        "effect_verified": False,
                        "no_effect": True,
                        "before_url": before,
                        "after_url": after,
                    },
                )
            return LayerResult(
                ok=True,
                layer=self.name,
                message=f"{atype} -> {after}",
                data={
                    "dispatched": dispatch_ok,
                    "effect_verified": True,
                    "before_url": before,
                    "after_url": after,
                },
            )

        if atype == "wait":
            ms = _wait_ms(params)
            await page.wait_for_timeout(ms)
            return LayerResult(ok=True, layer=self.name, message=f"waited {ms}ms")

        if atype == "press_key":
            # Distinguish "event dispatched" from "action had an effect".
            # Playwright's page.keyboard.press only fires a DOM key event; for
            # navigation-style chords (Alt/Meta+ArrowLeft, etc.) that is NOT a
            # browser back — the old code returned `ok=True` unconditionally,
            # which is what masked the absence of a real go_back for three key
            # attempts. We verify URL/history/document change so a no-op key no
            # longer claims success.
            key = params.get("key")
            if not key:
                return LayerResult(ok=False, layer=self.name, message="press_key needs params.key")
            before = page.url
            try:
                before_history = await page.evaluate("() => history.length")
            except Exception:
                before_history = None
            before_gen = await self._document_generation()
            try:
                await page.keyboard.press(key)
                dispatched = True
            except Exception as e:  # noqa: BLE001
                return LayerResult(
                    ok=False,
                    layer=self.name,
                    message=f"press_key dispatch failed: {type(e).__name__}: {e}",
                    data={"dispatched": False, "effect_verified": False},
                )
            # Give the page a beat to react (navigation, dialog, value change).
            await asyncio.sleep(0.05)
            try:
                after_history = await page.evaluate("() => history.length")
            except Exception:
                after_history = None
            after = page.url
            after_gen = await self._document_generation()
            moved = (
                after != before
                or (before_history is not None and after_history is not None and before_history != after_history)
                or (after_gen != before_gen)
            )
            if moved:
                try:
                    from qcu.session import invalidate_observation

                    invalidate_observation(current_url=after, document_changed=True)
                except Exception:
                    pass
            # Browser back/close chords are common: report effect_verified so
            # callers can branch on "the page did move" vs "key was sent but
            # nothing changed".
            return LayerResult(
                ok=True,
                layer=self.name,
                message=f"pressed {key}" + ("" if moved else " (no observable effect)"),
                data={
                    "dispatched": dispatched,
                    "effect_verified": moved,
                    "no_effect": not moved,
                    "before_url": before,
                    "after_url": after,
                },
            )

        if atype == "scroll":
            dx = int(params.get("dx", 0))
            dy = int(params.get("dy", 0))
            await page.mouse.wheel(dx, dy)
            return LayerResult(ok=True, layer=self.name, message=f"scrolled ({dx},{dy})")

        if atype == "type":
            text = params.get("text") or ""
            await page.keyboard.type(text)
            return LayerResult(ok=True, layer=self.name, message=f"typed {len(text)} chars")

        if atype in ("click", "double_click", "hover"):
            return await self._ref_action(page, atype, params)

        if atype == "fill":
            return await self._fill(page, params)

        if atype == "drag":
            return await self._drag(page, params)

        if atype == "screenshot":
            path = params.get("path")
            opts: dict[str, Any] = {"path": path} if path else {}
            await page.screenshot(**opts)
            return LayerResult(
                ok=True,
                layer=self.name,
                message="screenshot saved",
                data={"path": path} if path else {},
            )

        return LayerResult(ok=False, layer=self.name, message=f"unsupported action type: {atype}")

    async def _document_generation(self) -> Optional[int]:
        """Best-effort document identity token, for distinguishing real
        navigation from in-place DOM updates even when the URL is unchanged."""
        if self._page is None:
            return None
        try:
            return await self._page.evaluate(
                "() => (document && document.documentElement && document.documentElement.dataset)"
                    and "((document.documentElement.dataset.qcuDocGen) || null)"
            )
        except Exception:
            return None

    def _ref_current(self, ref: str) -> bool:
        """Reject refs that belong to a different observation generation.

        Refs are now prefixed with the observation they came from
        (``obs_N:ref_M``). After any navigation or DOM replacement the session
        observation_generation advances, so a stale ``obs_N`` token no longer
        matches the live document and must not be replayed — clicking it would
        target whatever element happens to reuse that index on the new page.
        """
        if not ref or ":" not in ref:
            # Legacy/unprefixed ref: only allow if no generation tracking is set
            # up (e.g. during a unit test that never observed).
            return True
        try:
            from qcu.session import load as _load

            s = _load()
            current = int(getattr(s, "observation_generation", 0) or 0) if s else 0
        except Exception:
            return True
        try:
            ref_gen = int(ref.split(":", 1)[0].replace("obs_", ""))
        except (ValueError, IndexError):
            return True
        # The latest observe set generation = current; an older observe has a
        # strictly smaller token.
        return ref_gen >= current

    # ------------------------------------------------------------------
    # DOM ref injection (best-effort, idempotent)
    # ------------------------------------------------------------------

    async def _ensure_ref_injected(self, page: Any, ref: str) -> bool:
        """Make sure ``ref`` is present as ``data-llm-ref`` on a live DOM node.

        Returns True if the attribute is now set on some node. We try the
        cheap path (CSS locator first) and only re-inject via CDP if the
        attribute is missing — this is the case after a fresh process
        spawn where the previous browser session's DOM annotations are
        gone.
        """
        # Fast path: already injected by a recent observe() in this process.
        try:
            loc = page.locator(f"[data-llm-ref={json.dumps(ref)}]")
            if await loc.count() > 0:
                return True
        except Exception:
            pass

        cached = self._last_refs.get(ref)
        if cached is None:
            return False
        bid = cached.get("backend_id")
        if bid is None or self._cdp is None:
            return False
        try:
            remote = (await self._cdp.send("DOM.resolveNode", {"backendNodeId": bid}))["object"]
            # callFunctionOn binds the resolved node to `this`, NOT to a
            # parameter. The old `function(el){el.setAttribute(...)}` form set
            # the attribute on the *ref string* (the `el` parameter received the
            # ref), so reinjection always failed silently and forced every
            # click onto the stale coordinate fallback. Using `this` is what
            # actually injects the attribute.
            await self._cdp.send(
                "Runtime.callFunctionOn",
                {
                    "objectId": remote["objectId"],
                    "functionDeclaration": (
                        "function(ref){ this.setAttribute('data-llm-ref', ref); }"
                    ),
                    "arguments": [{"value": ref}],
                },
            )
            await self._cdp.send("Runtime.releaseObject", {"objectId": remote["objectId"]})
        except Exception:
            return False
        return True

    async def _ref_action(self, page: Any, atype: str, params: dict[str, Any]) -> LayerResult:
        ref = params.get("ref")
        if not isinstance(ref, str):
            if "x" in params and "y" in params:
                x, y = float(params["x"]), float(params["y"])
            else:
                return LayerResult(ok=False, layer=self.name, message="click needs ref or x,y")
            if atype == "double_click":
                await page.mouse.dblclick(x, y)
            elif atype == "hover":
                await page.mouse.move(x, y)
            else:
                await page.mouse.click(x, y)
            return LayerResult(ok=True, layer=self.name, message=f"{atype} at ({x},{y})")

        cached = self._last_refs.get(ref)
        if cached is None:
            return LayerResult(
                ok=False, layer=self.name, message=f"unknown ref {ref!r}; re-run `qcu observe`"
            )

        # Stale ref: this ref was issued by an older observation; the page has
        # since changed (navigation/history move). Reusing it would silently
        # target whatever element reuses its index on the current page.
        if not self._ref_current(ref):
            return LayerResult(
                ok=False,
                layer=self.name,
                message=f"stale ref {ref!r}; re-run `qcu observe` before acting",
                data={"reason": "stale_ref"},
            )

        # Re-inject if needed (cross-process DOM reset).
        await self._ensure_ref_injected(page, ref)

        # Prefer DOM-locator click (robust to layout shift). Capture and
        # classify the failure reason instead of swallowing it — a strict-mode
        # ambiguity, a timeout, or a hidden/disabled target must not be silently
        # converted into a coordinate click on a possibly different element.
        loc = page.locator(f"[data-llm-ref={json.dumps(ref)}]")
        locator_count = 0
        try:
            locator_count = await loc.count()
        except Exception:
            locator_count = 0
        if locator_count > 0:
            try:
                if atype == "double_click":
                    await loc.dblclick()
                elif atype == "hover":
                    await loc.hover()
                else:
                    await loc.click()
                return LayerResult(ok=True, layer=self.name, message=f"{atype} ref={ref} (via locator)")
            except Exception as e:  # noqa: BLE001
                reason = _locator_failure(e)
                # Only fall back to coordinates when we have valid geometry that
                # belongs to THIS observation (cached.cx stamps the generation).
                cx, cy = cached.get("cx"), cached.get("cy")
                if cx is None or cy is None:
                    return LayerResult(
                        ok=False,
                        layer=self.name,
                        message=f"{atype} ref={ref} failed via locator ({reason}); no coordinate fallback",
                        data={"reason": reason, "locator_present": True},
                    )
                # Surface the degradation rather than hiding it.
                try:
                    await self._act_by_coord(page, atype, cx, cy)
                except Exception as e2:  # noqa: BLE001
                    return LayerResult(
                        ok=False,
                        layer=self.name,
                        message=f"{atype} ref={ref} locator={reason}, coord fallback failed: {e2}",
                        data={"reason": reason, "coord_fallback": True},
                    )
                return LayerResult(
                    ok=True,
                    layer=self.name,
                    message=f"{atype} ref={ref} (coord fallback after locator {reason})",
                    data={"reason": reason, "coord_fallback": True},
                )

        cx, cy = cached.get("cx"), cached.get("cy")
        if cx is None or cy is None:
            return LayerResult(
                ok=False, layer=self.name, message=f"ref {ref!r} has no geometry; try `qcu observe` again"
            )
        if atype == "double_click":
            await page.mouse.dblclick(cx, cy)
        elif atype == "hover":
            await page.mouse.move(cx, cy)
        else:
            await page.mouse.click(cx, cy)
        return LayerResult(
            ok=True, layer=self.name, message=f"{atype} ref={ref} (via coord)"
        )

    async def _act_by_coord(self, page: Any, atype: str, cx: float, cy: float) -> None:
        if atype == "double_click":
            await page.mouse.dblclick(cx, cy)
        elif atype == "hover":
            await page.mouse.move(cx, cy)
        else:
            await page.mouse.click(cx, cy)

    async def _resolve_endpoint(self, params: dict[str, Any], *, flat_x: str, flat_y: str,
                                ref_key: str, nested_key: str) -> tuple[Optional[float], Optional[float]]:
        """Resolve a drag endpoint to (x, y) in CSS pixels.

        Priority: params[flat_x/flat_y] > params[nested_key].{x,y} >
        params[ref_key] → cached cx/cy. Returns (None, None) if unresolvable.
        """
        x: Optional[float] = params.get(flat_x)
        y: Optional[float] = params.get(flat_y)
        if x is None or y is None:
            nested = params.get(nested_key)
            if isinstance(nested, dict):
                x = x if x is not None else nested.get("x")
                y = y if y is not None else nested.get("y")
        ref = params.get(ref_key)
        if (x is None or y is None) and isinstance(ref, str):
            cached = self._last_refs.get(ref)
            if cached is not None:
                x = cached.get("cx") if x is None else x
                y = cached.get("cy") if y is None else y
        if x is None or y is None:
            return None, None
        return float(x), float(y)

    async def _drag(self, page: Any, params: dict[str, Any]) -> LayerResult:
        """Press-drag-release via Playwright's mouse.

        Mirrors the desktop drag params (ref_from/ref_to, from/to, x1..y2).
        Uses ``page.mouse.move/down/move.../up`` with intermediate steps so
        DnD targets and sliders register a real drag, not a teleporting click.
        Verification is marked n/a — web has no cheap "did it move" probe
        comparable to AXPosition, so we are honest rather than guessing.
        """
        x1, y1 = await self._resolve_endpoint(params, flat_x="x1", flat_y="y1",
                                              ref_key="ref_from", nested_key="from")
        x2, y2 = await self._resolve_endpoint(params, flat_x="x2", flat_y="y2",
                                              ref_key="ref_to", nested_key="to")
        if x1 is None or y1 is None or x2 is None or y2 is None:
            return LayerResult(
                ok=False, layer=self.name,
                message="drag needs both endpoints: ref_from+ref_to or x1,y1,x2,y2",
            )
        duration = max(0.0, float(params.get("duration", 0.4)))
        steps = max(1, int(params.get("steps", 20)))
        step_sleep = duration / steps if steps > 0 else 0.0
        moved_px = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        try:
            await page.mouse.move(x1, y1)
            await page.mouse.down()
            for i in range(1, steps + 1):
                t = i / steps
                await page.mouse.move(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, steps=1)
                if step_sleep:
                    await page.wait_for_timeout(int(step_sleep * 1000))
            await page.mouse.up()
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"{type(e).__name__}: {e}")
        return LayerResult(
            ok=True, layer=self.name,
            message=f"drag ({x1:.0f},{y1:.0f})→({x2:.0f},{y2:.0f}) over {duration:.2f}s",
            data={"verification": {"verified": "n/a", "moved_px": round(moved_px, 1)}},
        )

    async def _fill(self, page: Any, params: dict[str, Any]) -> LayerResult:
        ref = params.get("ref")
        text = params.get("text", "")
        if not isinstance(ref, str):
            return LayerResult(ok=False, layer=self.name, message="fill needs params.ref")

        cached = self._last_refs.get(ref)
        if cached is None:
            return LayerResult(
                ok=False, layer=self.name, message=f"unknown ref {ref!r}; re-run `qcu observe`"
            )
        if not self._ref_current(ref):
            return LayerResult(
                ok=False,
                layer=self.name,
                message=f"stale ref {ref!r}; re-run `qcu observe` before acting",
                data={"reason": "stale_ref"},
            )

        # Re-inject the attribute so the locator can find the node.
        await self._ensure_ref_injected(page, ref)

        # Primary path: locator.fill() (sets the value AND fires input events).
        # Classify failures so we don't silently downgrade a strict-mode /
        # disabled-field error into a coordinate keyboard fill.
        loc = page.locator(f"[data-llm-ref={json.dumps(ref)}]")
        locator_count = 0
        try:
            locator_count = await loc.count()
        except Exception:
            locator_count = 0
        if locator_count > 0:
            try:
                await loc.fill(str(text))
                return LayerResult(
                    ok=True,
                    layer=self.name,
                    message=f"filled ref={ref} with {len(str(text))} chars (locator)",
                )
            except Exception as e:  # noqa: BLE001
                reason = _locator_failure(e)
                cx, cy = cached.get("cx"), cached.get("cy")
                if cx is None or cy is None:
                    return LayerResult(
                        ok=False,
                        layer=self.name,
                        message=f"fill ref={ref} failed via locator ({reason}); no coordinate fallback",
                        data={"reason": reason, "locator_present": True},
                    )
                # fall through to coord fallback but record the downgrade

        # Fallback path: click at cached coords, select-all, type via keyboard.
        # Works for any field even if the DOM annotation didn't survive.
        cx, cy = cached.get("cx"), cached.get("cy")
        if cx is None or cy is None:
            return LayerResult(
                ok=False,
                layer=self.name,
                message=f"ref {ref!r} not in DOM and has no geometry; try `qcu observe` again",
            )
        try:
            await page.mouse.click(cx, cy)
            # Select-all + type. Works for inputs, textareas, contentEditable.
            mod = "Meta" if _is_mac() else "Control"
            await page.keyboard.press(f"{mod}+A")
            await page.keyboard.press("Delete")
            if text:
                await page.keyboard.type(str(text))
            return LayerResult(
                ok=True,
                layer=self.name,
                message=f"filled ref={ref} with {len(str(text))} chars (coord+keyboard)",
            )
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"fill fallback failed: {e}")