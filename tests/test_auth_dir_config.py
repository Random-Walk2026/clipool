"""认证目录配置加载行为。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run_auth_dir(cwd: Path, home: Path, *, env_auth_dir: str | None = None) -> str:
    env = os.environ.copy()
    # 本仓库是 flat layout（ROOT/clipool），不是 src layout；子进程会切到临时 cwd，
    # 必须显式把真实仓库根放进 PYTHONPATH 才能验证 dotenv / HOME 行为。
    env["PYTHONPATH"] = str(ROOT)
    env["HOME"] = str(home)
    env.pop("CLIPOOL_AUTH_DIR", None)
    if env_auth_dir is not None:
        env["CLIPOOL_AUTH_DIR"] = env_auth_dir

    code = "from clipool.account import AUTH_DIR; print(AUTH_DIR)"
    res = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return res.stdout.strip()


class AuthDirConfig(unittest.TestCase):
    def test_auth_dir_from_environment_expands_user(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            home.mkdir()
            got = _run_auth_dir(Path(tmp), home, env_auth_dir="~/.clipool")
            self.assertEqual(got, str(home / ".clipool"))

    def test_dotenv_auth_dir_is_loaded_before_account_module(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            (root / ".env").write_text(
                textwrap.dedent(
                    """
                    # local auth directory
                    CLIPOOL_AUTH_DIR=~/.clipool
                    """
                ).strip()
                + "\n",
                encoding="utf-8",
            )

            got = _run_auth_dir(root, home)
            self.assertEqual(got, str(home / ".clipool"))

    def test_environment_auth_dir_wins_over_dotenv(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            home = root / "home"
            home.mkdir()
            (root / ".env").write_text(
                "CLIPOOL_AUTH_DIR=~/.from-dotenv\n", encoding="utf-8"
            )

            got = _run_auth_dir(root, home, env_auth_dir="~/.from-env")
            self.assertEqual(got, str(home / ".from-env"))


if __name__ == "__main__":
    unittest.main()
