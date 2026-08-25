from __future__ import annotations

import subprocess
from pathlib import Path


root = Path(__file__).resolve().parent
executable = root / ".venv" / "Scripts" / "python.exe"
logs = root / "logs"
logs.mkdir(exist_ok=True)
flags = (
    subprocess.DETACHED_PROCESS
    | subprocess.CREATE_NEW_PROCESS_GROUP
    | subprocess.CREATE_BREAKAWAY_FROM_JOB
    | subprocess.CREATE_NO_WINDOW
)
with (logs / "overnight.stdout.log").open("ab", buffering=0) as stdout, (
    logs / "overnight.stderr.log"
).open("ab", buffering=0) as stderr:
    process = subprocess.Popen(
        [str(executable), "-u", str(root / "run.py"), "--no-panel"],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        creationflags=flags,
        close_fds=True,
    )
print(process.pid)
