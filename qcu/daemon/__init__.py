"""Resident Python CLI daemon package.

The daemon hosts the QCU layer singletons (web_a11y, desktop_ax, ...) in one
long-lived process so that:

- per-command Python startup cost is paid once, not on every ``qcu`` call;
- desktop AX state (target app, enhanced-ui flag, live element refs) survives
  across CLI invocations, defeating the focus-drift problem.

See ``server.py`` for the RPC contract and ``launcher.py`` for process
lifecycle.
"""
