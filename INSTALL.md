# Install QCU 1.11.0

Python 3.11 or newer. Install into the environment that runs `qcu`; a different
Python may have different dependencies and macOS permission identity.

## Source

- Web: `python -m pip install -e .`, then `python -m playwright install chromium`.
- macOS: `python -m pip install -e '.[macos,dev]'`.
- Windows PowerShell: `py -m pip install -e ".[windows,dev]"`.
- Linux: install system GI/AT-SPI, then `python3 -m venv --system-site-packages .venv`
  and `.venv/bin/python -m pip install -e '.[linux,dev]'`.
  See [Linux setup](references/linux.md) for the distribution packages and session requirements.

For GNOME 46/50 Wayland native fallback, run `python -m qcu.gnome_bridge install`
in that Linux environment, then explicitly enable `qcu@quick-computer-use`
with `gnome-extensions enable`. A first install may need a logout/login for
GNOME discovery. AT-SPI semantic actions do not require this extension.

For an isolated Windows install from this directory:

```powershell
py -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -e ".[windows,dev]"
& .\.venv\Scripts\python.exe -m playwright install chromium # web/CDP fixtures
& .\scripts\qcu.ps1 --version
```

Activation and global PATH changes are unnecessary. `scripts\qcu.cmd` provides
the CMD entrypoint. Both prefer the local virtualenv, accept `QCU_PYTHON`, and
retain the resident daemon. Windowless pythonw is preferred when available so
the Windows venv launcher does not steal foreground with a new console.
Windows JSON streams use UTF-8; for PowerShell 5.1
use `$json | & .\scripts\qcu.ps1 act -` (or `batch -`) to preserve JSON quoting.

## Packaged wheel

```bash
python -m pip install 'qcu-1.11.0-py3-none-any.whl[macos]'
```

On Windows replace `[macos]` with `[windows]`, on Linux with `[linux]`; omit extras for web. Chromium is
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

## Windows validation

From the source/sdist root after installing `.[windows,dev]`:

```powershell
& .\.venv\Scripts\python.exe -X utf8 -m pytest -q
& .\.venv\Scripts\python.exe scripts\verify_windows_uia.py --output uia-result.json
& .\.venv\Scripts\python.exe scripts\verify_windows_native.py --output native-result.json --check-send-input
```

The live script launches a disposable native fixture, creates an isolated
QCU_HOME, and checks actual UIA patterns and postconditions. Use an interactive
Windows desktop. Save its JSON output and inspect failures; mock success is not
proof that the live test passed. Verified on Windows 10 build 19045 with
Python 3.12, uiautomation 2.0.29 and Playwright 1.64.0 on 2026-10-14.
The script checks direct and resident CLI execution, four semantic patterns,
Chinese/emoji, batch dispatch counts, stale refs and backend restart. The CDP
integration test uses a disposable Chromium endpoint; it does not prove that
every Electron/CEF app exposes one. Native screenshots use GDI/PrintWindow with
stdlib PNG encoding; mouse/keyboard fallback uses target-owned HWND messages
and offers explicit SendInput. No extra screenshot/input package is required.
Native validation uses a separate disposable fixture with clicks, double/right
click, hover, drag, wheel, Unicode typing, Ctrl+A selection and named keys,
plus target movement/occlusion/focus/stale-ref checks. Secure/elevated desktop
behavior and mixed monitor/DPI configurations remain outside this verification.
The optional SendInput probe reports its own result separately from the verified
HWND message channel; accepted events alone are not proof of delivery.
