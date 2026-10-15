import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.linux
@pytest.mark.integration
@pytest.mark.skipif(not sys.platform.startswith("linux") or os.environ.get("QCU_LINUX_LIVE")!="1",
                    reason="Opt in with QCU_LINUX_LIVE=1 in a disposable DBus/X11 desktop")
def test_real_gtk_atspi_and_x11_cli(tmp_path):
    root=Path(__file__).resolve().parents[1]
    result=subprocess.run([sys.executable,str(root/"scripts/verify_linux_atspi.py"),
        "--output",str(tmp_path/"linux-live.json")],cwd=root,capture_output=True,text=True,timeout=150)
    assert result.returncode==0,result.stdout+result.stderr+(tmp_path/"linux-live.json").read_text()
