"""Discover a CDP endpoint exposed by an external desktop app (Electron/CEF).

Electron and CEF apps speak the Chrome DevTools Protocol when launched with
``--remote-debugging-port``. When such an app is AX-opaque, attaching over CDP
yields the full DOM — the fast web path instead of screenshots.

Discovery is intentionally narrow and honest:

- only ports LISTENed by the *bound* pid are probed — never a host-wide scan;
- a candidate port must answer ``/json/version`` with a
  ``webSocketDebuggerUrl`` before it is treated as CDP;
- every failure (no tool, no ports, no CDP answer) is reported with a reason,
  never silently mapped onto another target.
"""
from __future__ import annotations

import json
import subprocess
import sys
from typing import Any, Callable, Optional

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Opener = Callable[..., Any]


def _default_runner(*args: Any, **kwargs: Any) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(*args, **kwargs)


def list_listening_ports(pid: int, *, runner: Runner = _default_runner) -> tuple[list[int], Optional[str]]:
    """Return ``(ports, error)``: TCP ports LISTENed by ``pid``.

    ``error`` is None on success (``ports`` may legitimately be empty) or a
    human-readable reason the lookup could not run.
    """
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return [], f"invalid pid: {pid!r}"
    try:
        if sys.platform == "darwin":
            res = runner(
                ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN", "-Fn"],
                capture_output=True, text=True, timeout=5,
            )
            if res.returncode not in (0, 1):  # lsof exits 1 when nothing matched
                return [], f"lsof exited {res.returncode}: {(res.stderr or '').strip()[:200]}"
            ports = []
            for line in res.stdout.splitlines():
                # lsof -Fn emits "p<pid>" and "n<name>" records; names look like
                # "*:9222 (LISTEN)" or "127.0.0.1:9222" depending on flags.
                if line.startswith("n") and ":" in line:
                    port_part = line[1:].split(" ")[0].rsplit(":", 1)[-1]
                    if port_part.isdigit():
                        ports.append(int(port_part))
            return sorted(set(ports)), None
        if sys.platform == "win32":
            res = runner(["netstat", "-ano", "-p", "tcp"], capture_output=True, text=True, timeout=10)
            if res.returncode != 0:
                return [], f"netstat exited {res.returncode}: {(res.stderr or '').strip()[:200]}"
            ports = []
            for line in res.stdout.splitlines():
                parts = line.split()
                # TCP  127.0.0.1:9222  0.0.0.0:0  LISTENING  12345
                if len(parts) >= 5 and parts[-1] == str(pid) and parts[-2].upper() == "LISTENING":
                    port_part = parts[1].rsplit(":", 1)[-1]
                    if port_part.isdigit():
                        ports.append(int(port_part))
            return sorted(set(ports)), None
        # Linux and others: parse ss; missing tool is a reported error.
        res = runner(["ss", "-ltnp"], capture_output=True, text=True, timeout=5)
        if res.returncode != 0:
            return [], f"ss exited {res.returncode}: {(res.stderr or '').strip()[:200]}"
        ports = []
        needle = f"pid={pid},"
        for line in res.stdout.splitlines():
            if needle in line:
                parts = line.split()
                if len(parts) >= 4:
                    port_part = parts[3].rsplit(":", 1)[-1].rstrip("]")
                    if port_part.isdigit():
                        ports.append(int(port_part))
        return sorted(set(ports)), None
    except FileNotFoundError as exc:
        return [], f"port discovery tool missing: {exc.filename}"
    except subprocess.TimeoutExpired:
        return [], "port discovery timed out"
    except Exception as exc:  # noqa: BLE001
        return [], f"port discovery failed: {type(exc).__name__}: {exc}"


def probe_cdp_version(port: int, *, timeout: float = 0.8, opener: Optional[Opener] = None) -> Optional[dict[str, Any]]:
    """GET ``http://127.0.0.1:<port>/json/version``; return parsed info only
    when the response really is a CDP endpoint (has webSocketDebuggerUrl)."""
    if opener is None:
        from urllib.request import urlopen

        def opener(url: str, *, timeout: float) -> Any:  # type: ignore[no-redef]
            return urlopen(url, timeout=timeout)

    try:
        with opener(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — not HTTP, not JSON, refused: not CDP
        return None
    if not isinstance(data, dict) or not data.get("webSocketDebuggerUrl"):
        return None
    return {
        "port": port,
        "url": f"http://127.0.0.1:{port}",
        "browser": str(data.get("Browser") or ""),
        "protocol_version": str(data.get("Protocol-Version") or ""),
    }


def find_cdp_endpoint(pid: int, *, runner: Runner = _default_runner,
                      opener: Optional[Opener] = None) -> tuple[Optional[dict[str, Any]], dict[str, Any]]:
    """Find the bound app's CDP endpoint. Returns ``(endpoint, diagnostics)``.

    ``endpoint`` is ``{"port", "url", "browser", ...}`` or None. Diagnostics
    always explain the outcome: ports checked, lookup errors, and a hint when
    the app simply was not launched with ``--remote-debugging-port``.
    """
    ports, error = list_listening_ports(pid, runner=runner)
    diag: dict[str, Any] = {"pid": pid, "ports_checked": ports}
    if error:
        diag["error"] = error
        return None, diag
    for port in ports:
        info = probe_cdp_version(port, opener=opener)
        if info is not None:
            diag["browser"] = info["browser"]
            return info, diag
    diag["reason"] = ("no_cdp_endpoint" if ports else "no_listening_ports")
    diag["hint"] = ("relaunch the app with --remote-debugging-port=<port> "
                    "(Electron/CEF) to enable the CDP bridge")
    return None, diag
