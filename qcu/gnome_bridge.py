"""Install the optional QCU GNOME extension for the current desktop user."""
from __future__ import annotations

import argparse
from importlib.resources import files
import os
from pathlib import Path
import subprocess
import sys

UUID = "qcu@quick-computer-use"


def install(*, data_home=None, enable=False):
    if not sys.platform.startswith("linux"):
        raise RuntimeError("Install the GNOME bridge on the Linux desktop, as its logged-in user")
    root = Path(data_home or os.environ.get("XDG_DATA_HOME") or Path.home()/".local/share")
    destination = root/"gnome-shell/extensions"/UUID
    destination.mkdir(parents=True, exist_ok=True)
    source = files("qcu").joinpath("assets", "gnome", UUID)
    for name in ("metadata.json", "extension.js"):
        (destination/name).write_bytes(source.joinpath(name).read_bytes())
    print(f"Installed {destination}")
    print("This extension grants QCU native window capture and mouse/keyboard access to your GNOME session.")
    print("Enable with: gnome-extensions enable " + UUID)
    print("For a first install GNOME may need a logout/login before it discovers the extension.")
    if enable:
        subprocess.run(["gnome-extensions", "enable", UUID], check=True, timeout=15)
    return destination


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install"])
    parser.add_argument("--enable", action="store_true", help="also ask GNOME to enable the installed extension")
    args = parser.parse_args()
    install(enable=args.enable)


if __name__ == "__main__":
    main()
