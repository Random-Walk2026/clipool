from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipool.providers import antigravity


def _completed(args: list[str], returncode: int = 0, stderr: str = ""):
    return subprocess.CompletedProcess(args, returncode, "", stderr)


def _working_security(calls: list[list[str]], *, reject_unlock: Path | None = None):
    def fake_security(args: list[str], home: str):
        calls.append(args)
        keychain = Path(args[-1])
        if args[0] == "create-keychain":
            keychain.write_text("dedicated-keychain", encoding="utf-8")
        if args[0] == "unlock-keychain" and keychain == reject_unlock:
            return _completed(args, 1, "bad password")
        return _completed(args)

    return fake_security


def _write_valid_token(profile: Path) -> Path:
    token_file = profile / antigravity._AGY_TOKEN_RELATIVE
    token_file.parent.mkdir(parents=True)
    token_file.write_text(
        json.dumps(
            {
                "auth_method": "oauth",
                "token": {
                    "access_token": "test-access",
                    "refresh_token": "test-refresh",
                },
            }
        ),
        encoding="utf-8",
    )
    return token_file


class ProfileKeychainSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        antigravity._prepared_homes.clear()

    def tearDown(self) -> None:
        antigravity._prepared_homes.clear()

    def test_real_home_is_never_touched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real_home = Path(tmp) / "real-home"
            root = Path(tmp) / "auth" / "profiles"
            keychain = real_home / antigravity._LEGACY_PROFILE_KEYCHAIN_RELATIVE
            keychain.parent.mkdir(parents=True)
            keychain.write_text("real credentials", encoding="utf-8")

            with (
                patch.object(antigravity.sys, "platform", "darwin"),
                patch.object(antigravity, "_real_user_home", return_value=real_home),
                patch.object(antigravity, "_managed_profile_root", return_value=root),
                patch.object(antigravity, "_security") as security,
            ):
                with self.assertRaisesRegex(RuntimeError, "真实用户 HOME"):
                    antigravity.ensure_profile_keychain(str(real_home))

            self.assertEqual(keychain.read_text(encoding="utf-8"), "real credentials")
            security.assert_not_called()

    def test_external_unmarked_profile_fails_closed_without_popup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = Path(tmp) / "external-profile"
            profile.mkdir()
            root = Path(tmp) / "auth" / "profiles"

            with (
                patch.object(antigravity.sys, "platform", "darwin"),
                patch.object(
                    antigravity, "_real_user_home", return_value=Path(tmp) / "real"
                ),
                patch.object(antigravity, "_managed_profile_root", return_value=root),
                patch.object(antigravity, "_security") as security,
            ):
                with self.assertRaisesRegex(RuntimeError, "不会启动 agy"):
                    antigravity.ensure_profile_keychain(str(profile))

            security.assert_not_called()

    def test_dedicated_keychain_is_private_default_and_legacy_is_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "auth" / "profiles"
            profile = root / "agy-test"
            legacy = profile / antigravity._LEGACY_PROFILE_KEYCHAIN_RELATIVE
            legacy.parent.mkdir(parents=True)
            legacy.write_text("unknown-login-keychain", encoding="utf-8")
            calls: list[list[str]] = []

            with (
                patch.object(antigravity.sys, "platform", "darwin"),
                patch.object(
                    antigravity, "_real_user_home", return_value=Path(tmp) / "real"
                ),
                patch.object(antigravity, "_managed_profile_root", return_value=root),
                patch.object(
                    antigravity, "_security", side_effect=_working_security(calls)
                ),
            ):
                antigravity.ensure_profile_keychain(str(profile))
                call_count = len(calls)
                antigravity.ensure_profile_keychain(str(profile))

            self.assertEqual(len(calls), call_count)
            self.assertEqual(legacy.read_text(encoding="utf-8"), "unknown-login-keychain")
            state_path = profile / antigravity._PROFILE_KEYCHAIN_STATE_RELATIVE
            state = json.loads(state_path.read_text(encoding="utf-8"))
            keychain = profile / state["keychain"]
            self.assertTrue(keychain.name.startswith("clipool-"))
            self.assertTrue(state["password"])
            self.assertEqual(state_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(keychain.stat().st_mode & 0o777, 0o600)
            self.assertEqual(profile.stat().st_mode & 0o777, 0o700)
            self.assertEqual(keychain.parent.stat().st_mode & 0o777, 0o700)
            self.assertIn(
                ["default-keychain", "-d", "user", "-s", str(keychain.resolve())], calls
            )
            self.assertIn(
                ["list-keychains", "-d", "user", "-s", str(keychain.resolve())], calls
            )

    def test_bad_dedicated_keychain_is_archived_and_rebuilt_with_new_name(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "auth" / "profiles"
            profile = root / "agy-test"
            calls: list[list[str]] = []

            with (
                patch.object(antigravity.sys, "platform", "darwin"),
                patch.object(
                    antigravity, "_real_user_home", return_value=Path(tmp) / "real"
                ),
                patch.object(antigravity, "_managed_profile_root", return_value=root),
                patch.object(
                    antigravity, "_security", side_effect=_working_security(calls)
                ),
            ):
                antigravity.ensure_profile_keychain(str(profile))

            old_state_path = profile / antigravity._PROFILE_KEYCHAIN_STATE_RELATIVE
            old_state = json.loads(old_state_path.read_text(encoding="utf-8"))
            old_keychain = profile / old_state["keychain"]
            antigravity._prepared_homes.clear()
            calls.clear()

            with (
                patch.object(antigravity.sys, "platform", "darwin"),
                patch.object(
                    antigravity, "_real_user_home", return_value=Path(tmp) / "real"
                ),
                patch.object(antigravity, "_managed_profile_root", return_value=root),
                patch.object(
                    antigravity,
                    "_security",
                    side_effect=_working_security(
                        calls, reject_unlock=old_keychain.resolve()
                    ),
                ),
            ):
                antigravity.ensure_profile_keychain(str(profile))

            new_state = json.loads(old_state_path.read_text(encoding="utf-8"))
            self.assertNotEqual(new_state["keychain"], old_state["keychain"])
            self.assertEqual(len(list(old_keychain.parent.glob(f"{old_keychain.name}.clipool-backup-*"))), 1)
            self.assertEqual(len(list(profile.glob(".clipool-keychain.json.clipool-backup-*"))), 1)

    def test_nested_keychain_symlink_fails_before_security_or_real_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "auth" / "profiles"
            profile = root / "agy-test"
            real_library = Path(tmp) / "real-home" / "Library"
            real_keychains = real_library / "Keychains"
            real_keychains.mkdir(parents=True)
            profile.mkdir(parents=True)
            os.symlink(real_library, profile / "Library")

            with (
                patch.object(antigravity.sys, "platform", "darwin"),
                patch.object(
                    antigravity, "_real_user_home", return_value=Path(tmp) / "real-home"
                ),
                patch.object(antigravity, "_managed_profile_root", return_value=root),
                patch.object(antigravity, "_security") as security,
            ):
                with self.assertRaisesRegex(RuntimeError, "符号链接"):
                    antigravity.ensure_profile_keychain(str(profile))

            security.assert_not_called()
            self.assertEqual(list(real_keychains.iterdir()), [])

    def test_preferences_symlink_fails_before_default_keychain_write(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "auth" / "profiles"
            profile = root / "agy-test"
            real_preferences = Path(tmp) / "real-home" / "Library" / "Preferences"
            real_preferences.mkdir(parents=True)
            (profile / "Library").mkdir(parents=True)
            os.symlink(real_preferences, profile / "Library" / "Preferences")

            with (
                patch.object(antigravity.sys, "platform", "darwin"),
                patch.object(
                    antigravity, "_real_user_home", return_value=Path(tmp) / "real-home"
                ),
                patch.object(antigravity, "_managed_profile_root", return_value=root),
                patch.object(antigravity, "_security") as security,
            ):
                with self.assertRaisesRegex(RuntimeError, "符号链接"):
                    antigravity.ensure_profile_keychain(str(profile))

            security.assert_not_called()
            self.assertEqual(list(real_preferences.iterdir()), [])

    def test_security_failure_stops_before_agy_can_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "auth" / "profiles"
            profile = root / "agy-test"
            profile.mkdir(parents=True)

            with (
                patch.object(antigravity.sys, "platform", "darwin"),
                patch.object(
                    antigravity, "_real_user_home", return_value=Path(tmp) / "real"
                ),
                patch.object(antigravity, "_managed_profile_root", return_value=root),
                patch.object(
                    antigravity,
                    "_security",
                    return_value=_completed(["security"], 1, "cannot create"),
                ),
            ):
                with self.assertRaisesRegex(RuntimeError, "无法创建"):
                    antigravity.ensure_profile_keychain(str(profile))

            self.assertNotIn(str(profile.resolve()), antigravity._prepared_homes)

    def test_provider_without_isolated_home_never_starts_agy(self) -> None:
        provider = antigravity.AntigravityProvider()
        with patch("clipool.providers.base.BaseProvider.run") as base_run:
            with self.assertRaisesRegex(RuntimeError, "不会启动 agy"):
                provider.run("hello", env_override=None)
        base_run.assert_not_called()

    def test_validated_token_is_private_and_stays_inside_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "auth" / "profiles"
            profile = root / "agy-test"
            token_file = _write_valid_token(profile)
            with (
                patch.object(
                    antigravity, "_real_user_home", return_value=Path(tmp) / "real"
                ),
                patch.object(antigravity, "_managed_profile_root", return_value=root),
            ):
                resolved = antigravity.validated_profile_token_file(str(profile))

            self.assertEqual(resolved, token_file.resolve())
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(token_file.parent.stat().st_mode & 0o777, 0o700)

    def test_token_directory_symlink_cannot_escape_to_real_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "auth" / "profiles"
            profile = root / "agy-test"
            real_profile = Path(tmp) / "real-profile"
            real_token = _write_valid_token(real_profile)
            profile.mkdir(parents=True)
            os.symlink(real_profile / ".gemini", profile / ".gemini")

            with (
                patch.object(
                    antigravity, "_real_user_home", return_value=Path(tmp) / "real-home"
                ),
                patch.object(antigravity, "_managed_profile_root", return_value=root),
            ):
                with self.assertRaisesRegex(RuntimeError, "token 目录符号链接"):
                    antigravity.validated_profile_token_file(str(profile))

            self.assertEqual(real_token.read_text(encoding="utf-8").count("test-access"), 1)

    def test_corrupt_token_stops_before_agy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "auth" / "profiles"
            profile = root / "agy-test"
            token_file = profile / antigravity._AGY_TOKEN_RELATIVE
            token_file.parent.mkdir(parents=True)
            token_file.write_text("not-json", encoding="utf-8")

            with (
                patch.object(
                    antigravity, "_real_user_home", return_value=Path(tmp) / "real"
                ),
                patch.object(antigravity, "_managed_profile_root", return_value=root),
                patch("clipool.providers.base.BaseProvider.run") as base_run,
            ):
                with self.assertRaisesRegex(RuntimeError, "token JSON 损坏"):
                    antigravity.AntigravityProvider().run(
                        "hello", env_override={"HOME": str(profile)}
                    )
            base_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
