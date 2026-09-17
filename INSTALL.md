# Install QCU 1.8

Python 3.11 or newer. Install into the environment that runs `qcu`; a different
Python may have different dependencies and macOS permission identity.

## Source

- Web: `python -m pip install -e .`, then `python -m playwright install chromium`.
- macOS: `python -m pip install -e '.[macos,dev]'`.
- Windows PowerShell: `py -m pip install -e ".[windows,dev]"`.

## Packaged wheel

```bash
python -m pip install 'qcu-1.8-py3-none-any.whl[macos]'
```

On Windows replace `[macos]` with `[windows]`; omit extras for web. Chromium is
needed only for web operations. UIA is loaded lazily on Windows on its own
persistent COM worker. The `windows` extra installs `uiautomation>=2.0`; it is
never required for macOS or web. The library's transitive `comtypes` dependency
provides the COM interfaces. No VM/cloud service or paid visual model is needed.

## Check the actual environment

`qcu --version` and `qcu doctor` show version, interpreter and import paths.
Doctor capabilities are preflight: `dependencies_present:true` does not prove
an interactive session or target is accessible. Observe a bounded test target
before attempting an action. macOS permission prompts need user interaction;
Windows secure/elevated desktops and inaccessible processes may fail.

Stop the task's daemon after an update so cached Python code is reloaded. Use an
independent `QCU_HOME` for tests; never purge a user's regular Chromium profile.
Re-observe all targets after upgrading from 1.x. Read [API migration](references/api.md).

## Windows validation (not yet run on real Windows)

From the source/sdist root after installing `.[windows,dev]`:

```powershell
py -m pytest -q tests/test_desktop_uia.py
py scripts/verify_windows_uia.py
```

The live script launches a disposable native fixture, creates an isolated
QCU_HOME, and checks actual UIA patterns and postconditions. Use an interactive
Windows desktop. Save its JSON output and inspect failures; mock success is not
proof that the live test passed. No general Windows support claim is made.
