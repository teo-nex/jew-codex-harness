"""Bounded, non-interactive checks without inherited output-pipe drain waits."""

import os
from pathlib import Path
import signal
import subprocess
import tempfile


OUTPUT_LIMIT = 8 * 1024 * 1024


def _stop_owned(process):
    if os.name == "nt":
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        try:
            subprocess.run([str(Path(system_root) / "System32/taskkill.exe"),
                            "/PID", str(process.pid), "/T", "/F"],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass


def run_bounded(command, *, cwd=None, env=None, timeout):
    """Capture a finite check; on timeout stop only its owned process tree."""
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt"
               else {"start_new_session": True})
    # Descendants can inherit pipes on Windows and prevent communicate() from
    # returning after the launcher dies. File capture never waits for pipe EOF.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        process = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, **options)
        try:
            process.wait(timeout=timeout)
        except BaseException:
            _stop_owned(process)
            raise
        output = []
        for stream in (stdout, stderr):
            stream.seek(0)
            data = stream.read(OUTPUT_LIMIT + 1)
            if len(data) > OUTPUT_LIMIT:
                raise OSError("check output exceeded capture limit")
            output.append(data.decode("utf-8", errors="replace"))
    return subprocess.CompletedProcess(command, process.returncode, *output)
