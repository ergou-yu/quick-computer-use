"""Command-line entry point.

This module is intentionally thin: it parses argv, dispatches to a handler,
and serializes results as JSON to stdout. Each handler is implemented in
``qcu.cli_handlers`` to keep ``cli.py`` easy to skim.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import NoReturn

from qcu import __version__


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="qcu",
        description="Quick Computer Use — a faster computer-use skill",
    )
    p.add_argument("--version", action="version", version=f"qcu {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    # session
    sp = sub.add_parser("session", help="manage the persistent session")
    ssub = sp.add_subparsers(dest="session_cmd", required=True)
    start = ssub.add_parser("start", help="start a new session")
    start.add_argument("--context", default="auto", choices=["auto", "web", "desktop"])
    mode = start.add_mutually_exclusive_group()
    mode.add_argument("--headed", action="store_true", help="show the browser window")
    mode.add_argument("--headless", action="store_true", help="hide the browser window")
    start.add_argument(
        "--strict-background", action="store_true",
        help="desktop: abort any action that would steal the foreground, and "
             "fail-fast if the frontmost app drifts mid-task. Detects drift "
             "only — true background control (AX without activation) is a "
             "future improvement; this flag prevents silent focus theft.",
    )
    start.add_argument(
        "--no-screenshot-fallback", action="store_true",
        help="hard-disable the screenshot fallback layer for this session. When "
             "the router would normally degrade to screenshot+vision (e.g. the "
             "target sits on a canvas, or no a11y ref resolves), the action is "
             "refused with ok=false instead. Use when you must guarantee the "
             "task never relies on vision.",
    )
    ssub.add_parser("status", help="print current session status")
    end = ssub.add_parser("end", help="close the current session and stop the browser daemon")
    end.add_argument("--purge-profile", action="store_true", help="delete QCU's browser profile")

    # browser — inspect/stop the long-lived Chromium daemon directly.
    bp = sub.add_parser("browser", help="inspect or stop the Chromium daemon")
    bsub = bp.add_subparsers(dest="browser_cmd", required=True)
    bsub.add_parser("status", help="show whether the daemon is alive and on which port")
    bsub.add_parser("stop", help="force-stop the daemon (even if session.json is gone)")

    # daemon — inspect/stop the resident Python CLI daemon (hosts layer
    # singletons; auto-started on first observe/act).
    dp = sub.add_parser("daemon", help="inspect or stop the resident Python CLI daemon")
    dsub = dp.add_subparsers(dest="daemon_cmd", required=True)
    dsub.add_parser("status", help="show daemon health, port/pid, and warm layer instances")
    dsub.add_parser("stop", help="force-stop the daemon (next command lazy-starts a fresh one)")

    # observe
    op = sub.add_parser("observe", help="capture the current observation")
    op.add_argument("--layer", default=None, help="force a specific layer (overrides the router)")
    op.add_argument("--max-depth", type=int, default=10)
    op.add_argument("--full-text", action="store_true", help="do not truncate raw tree text")
    op.add_argument("--text-limit", type=int, default=None, help="maximum raw tree characters")
    op.add_argument("--tail", action="store_true", help="take elements/text from the end")
    op.add_argument("--limit", type=int, default=None, help="maximum elements to return")
    op.add_argument("--offset", type=int, default=0, help="skip matching elements")
    op.add_argument("--compact", action="store_true", help="omit null and bulky fields")
    op.add_argument("--wait-until", choices=["domcontentloaded", "networkidle"], default="domcontentloaded",
                    help="web readiness policy; network idle is opt-in")
    op.add_argument("--wait-for", default=None, help="web: wait for this known CSS selector to be visible")
    op.add_argument("--timeout-ms", type=int, default=3000, help="web readiness timeout (1..60000 ms)")
    op.add_argument("--role", default=None, help="filter elements by exact role")
    op.add_argument("--name", default=None, help="filter elements by name substring")
    op.add_argument("--app", default=None,
                    help="desktop: observe this app by name (e.g. 'TextEdit') instead of the "
                         "frontmost one. Avoids inspecting the wrong app when QCU runs in the "
                         "background and focus has drifted.")
    op.add_argument("--pid", type=int, default=None,
                    help="desktop: observe the app with this process id (most precise).")
    op.add_argument("--window", default=None,
                    help="desktop: only walk the AXWindow whose title contains this substring, "
                         "dropping the rest of the app tree. Cuts hundreds of elements down to "
                         "the target window's controls.")

    # find / inspect use the same persisted observation contract as routing.
    fp = sub.add_parser("find", help="find elements in the latest real observation")
    fp.add_argument("--role", default=None)
    fp.add_argument("--name", default=None)
    fp.add_argument("--limit", type=int, default=None)
    fp.add_argument("--offset", type=int, default=0)
    fp.add_argument("--compact", action="store_true")
    ip = sub.add_parser("inspect", help="inspect a ref in the latest real observation")
    ip.add_argument("ref")
    ip.add_argument("--compact", action="store_true")

    # act
    ap = sub.add_parser("act", help="perform an action")
    ap.add_argument("action", help="action JSON, e.g. '{\"type\":\"click\",\"params\":{\"ref\":\"ref_42\"}}'")
    ap.add_argument("--layer", default=None, help="force a specific layer")

    bp = sub.add_parser("batch", help="run observed form fills and one final action in one call")
    bp.add_argument("actions", help="JSON array of 1..32 actions; only fills before the final action")
    bp.add_argument("--layer", default=None)
    bp.add_argument("--observe", action="store_true", help="return a fresh observation after execution")
    bp.add_argument("--compact", action="store_true", help="compact the final observation")

    # route
    rp = sub.add_parser("route", help="show routing decision for given features")
    rp.add_argument("features", help="features JSON file, or '-' for stdin")

    # stats
    sp_ = sub.add_parser("stats", help="summarize collected telemetry")
    sp_.add_argument("--since", default="7d", help="time window, e.g. '7d', '24h'")
    sp_.add_argument("--path", default=None, help="override telemetry path")

    # schema
    scp = sub.add_parser("schema", help="print a JSON schema")
    scp.add_argument("what", choices=["observation", "action"])

    # doctor — macOS TCC permission diagnostic for the desktop path.
    dcp = sub.add_parser(
        "doctor",
        help="probe macOS TCC permissions (Accessibility / Input Monitoring "
        "/ Screen Recording) and surface missing ones",
    )

    return p


def _die(msg: str, code: int = 1) -> NoReturn:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(code)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Lazy import to keep cold start cheap for `qcu --version`.
    from qcu import cli_handlers as h

    try:
        if args.cmd == "session":
            if args.session_cmd == "start":
                headless = None
                if getattr(args, "headed", False):
                    headless = False
                if getattr(args, "headless", False):
                    headless = True
                return h.session_start(args.context, headless=headless,
                                       strict_background=getattr(args, "strict_background", False),
                                       no_screenshot_fallback=getattr(args, "no_screenshot_fallback", False))
            if args.session_cmd == "status":
                return h.session_status()
            if args.session_cmd == "end":
                return h.session_end(purge_profile=getattr(args, "purge_profile", False))
            _die(f"unknown session sub-command: {args.session_cmd}")
        if args.cmd == "browser":
            if args.browser_cmd == "status":
                return h.browser_status()
            if args.browser_cmd == "stop":
                return h.browser_stop()
            _die(f"unknown browser sub-command: {args.browser_cmd}")
        if args.cmd == "daemon":
            if args.daemon_cmd == "status":
                return h.daemon_status()
            if args.daemon_cmd == "stop":
                return h.daemon_stop()
            _die(f"unknown daemon sub-command: {args.daemon_cmd}")
        if args.cmd == "observe":
            return h.observe(
                layer=args.layer,
                max_depth=args.max_depth,
                full_text=getattr(args, "full_text", False),
                text_limit=args.text_limit,
                tail=getattr(args, "tail", False),
                limit=args.limit,
                offset=getattr(args, "offset", 0),
                compact=getattr(args, "compact", False),
                role=args.role,
                name=args.name,
                app=getattr(args, "app", None),
                pid=getattr(args, "pid", None),
                window=getattr(args, "window", None),
                wait_until=args.wait_until,
                wait_for=args.wait_for,
                timeout_ms=args.timeout_ms,
            )
        if args.cmd == "find":
            return h.find(
                role=args.role,
                name=args.name,
                limit=args.limit,
                offset=getattr(args, "offset", 0),
                compact=getattr(args, "compact", False),
            )
        if args.cmd == "inspect":
            return h.inspect(args.ref, compact=getattr(args, "compact", False))
        if args.cmd == "act":
            return h.act(args.action, layer=args.layer)
        if args.cmd == "batch":
            return h.batch(args.actions, layer=args.layer, observe_after=args.observe, compact=args.compact)
        if args.cmd == "route":
            return h.route(args.features)
        if args.cmd == "stats":
            return h.stats(since=args.since, path=args.path)
        if args.cmd == "schema":
            return h.schema(args.what)
        if args.cmd == "doctor":
            return h.doctor()
        _die(f"unknown command: {args.cmd}")
    except KeyboardInterrupt:
        _die("interrupted", code=130)
    except Exception as e:  # noqa: BLE001 — top-level boundary
        _die(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    raise SystemExit(main())
