"""Daemon process entry point: ``python -m qcu.daemon --port 9423``.

Run by ``launcher.launch_daemon`` as a detached subprocess. Parses ``--port``
and blocks in ``server.serve`` until killed. Exits non-zero on bind failure so
the launcher's readiness poll fails fast and it can retry with a new port.
"""

from __future__ import annotations

import argparse
import sys

from qcu.daemon.server import serve


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="qcu-daemon", description="QCU resident CLI daemon")
    p.add_argument("--port", type=int, required=True, help="loopback TCP port to bind")
    args = p.parse_args(argv)
    # Mark THIS process as the daemon so handlers know NOT to route back into
    # us (which would recurse: daemon calls observe -> observe tries to route
    # to the daemon -> daemon calls observe ... ). Every per-command handler
    # checks this flag before attempting daemon routing.
    import os
    os.environ["QCU_DAEMON_PROCESS"] = "1"
    try:
        serve(args.port)
    except OSError as e:
        # Bind failure (port in use, permission, ...). The launcher will see
        # no health response and retry with a fresh port.
        print(f"qcu-daemon: bind failed on port {args.port}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
