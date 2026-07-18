from __future__ import annotations

import os
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from clipool.providers.base import BaseProvider


class _TestProvider(BaseProvider):
    name = "test"
    label = "Test"

    def _build_cmd(self, text: str, model: str, effort: str) -> list[str]:
        return []


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@unittest.skipIf(os.name == "nt", "process-group behavior is Unix-specific")
class SubprocessCleanupTests(unittest.TestCase):
    def test_timeout_terminates_grandchild_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "grandchild.pid"
            child_code = (
                "import subprocess,sys,time,pathlib;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid));"
                "time.sleep(30)"
            )
            grandchild_pid = 0
            try:
                with patch("clipool.providers.base.CLI_TIMEOUT", 0.5):
                    with self.assertRaisesRegex(RuntimeError, "调用超时"):
                        _TestProvider()._run_subprocess(
                            [sys.executable, "-c", child_code]
                        )

                grandchild_pid = int(pid_file.read_text(encoding="utf-8"))
                deadline = time.monotonic() + 2
                while _process_exists(grandchild_pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(
                    _process_exists(grandchild_pid),
                    "CLI timeout 后孙进程仍存活",
                )
            finally:
                if grandchild_pid and _process_exists(grandchild_pid):
                    try:
                        os.kill(grandchild_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    def test_timeout_kills_grandchild_that_ignores_sigterm(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pid_file = Path(tmp) / "stubborn-grandchild.pid"
            grandchild_code = (
                "import os,signal,time,pathlib;"
                "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()));"
                "time.sleep(30)"
            )
            child_code = (
                "import subprocess,sys,time;"
                f"subprocess.Popen([sys.executable,'-c',{grandchild_code!r}]);"
                "time.sleep(30)"
            )
            grandchild_pid = 0
            try:
                with patch("clipool.providers.base.CLI_TIMEOUT", 0.5):
                    with self.assertRaisesRegex(RuntimeError, "调用超时"):
                        _TestProvider()._run_subprocess(
                            [sys.executable, "-c", child_code]
                        )

                grandchild_pid = int(pid_file.read_text(encoding="utf-8"))
                deadline = time.monotonic() + 2
                while _process_exists(grandchild_pid) and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertFalse(
                    _process_exists(grandchild_pid),
                    "忽略 SIGTERM 的孙进程在 timeout 后仍存活",
                )
            finally:
                if grandchild_pid and _process_exists(grandchild_pid):
                    try:
                        os.kill(grandchild_pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass


if __name__ == "__main__":
    unittest.main()
