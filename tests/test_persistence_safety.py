from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from clipool import account as accountmod
from clipool.account import Account, _account_from_file, save_account


class AccountPersistenceSafetyTests(unittest.TestCase):
    def _account_file(self, root: Path) -> Path:
        path = root / "claude.json"
        path.write_text(
            json.dumps(
                {
                    "type": "claude",
                    "email": "user@example.com",
                    "access_token": "old-token",
                    "refresh_token": "old-refresh",
                    "enabled": True,
                }
            ),
            encoding="utf-8",
        )
        return path

    def test_stale_quota_writer_does_not_roll_back_refreshed_token(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._account_file(Path(tmp))
            token_writer = _account_from_file(path)
            stale_quota_writer = _account_from_file(path)
            self.assertIsNotNone(token_writer)
            self.assertIsNotNone(stale_quota_writer)

            token_writer.token = "fresh-token"
            token_writer.refresh_token = "fresh-refresh"
            self.assertTrue(
                token_writer.persist(fields={"token", "refresh_token"})
            )

            stale_quota_writer.quota = {"weekly": {"used_percent": 25}}
            stale_quota_writer.quota_updated_at = 123.0
            self.assertTrue(stale_quota_writer.persist(fields={"quota"}))

            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(data["token"], "fresh-token")
            self.assertEqual(data["access_token"], "fresh-token")
            self.assertEqual(data["refresh_token"], "fresh-refresh")
            self.assertEqual(data["quota"]["weekly"]["used_percent"], 25)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corrupt_source_is_preserved_instead_of_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.json"
            original = "{ definitely not json"
            path.write_text(original, encoding="utf-8")
            account = Account(
                backend="claude",
                id="broken",
                token="new-token",
                source_path=str(path),
            )

            self.assertFalse(account.persist(fields={"token"}))
            self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_non_utf8_source_is_preserved_instead_of_crashing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "binary.json"
            original = b"\xff\xfe\x00"
            path.write_bytes(original)
            account = Account(
                backend="claude",
                id="binary",
                token="new-token",
                source_path=str(path),
            )

            self.assertFalse(account.persist(fields={"token"}))
            self.assertEqual(path.read_bytes(), original)

    def test_deleted_source_is_not_recreated_by_stale_account(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._account_file(Path(tmp))
            account = _account_from_file(path)
            self.assertIsNotNone(account)
            path.unlink()

            account.token = "stale-refresh"
            self.assertFalse(account.persist(fields={"token"}))
            self.assertFalse(path.exists())

    def test_unknown_patch_field_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = self._account_file(Path(tmp))
            account = _account_from_file(path)
            self.assertIsNotNone(account)
            with self.assertRaisesRegex(ValueError, "未知持久化字段"):
                account.persist(fields={"everything"})

    def test_save_account_forces_private_permissions_under_common_umask(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = Path(tmp) / "auth"
            account = Account("claude", "user@example.com", token="secret")
            previous_umask = os.umask(0o022)
            try:
                with patch.object(accountmod, "AUTH_DIR", auth_dir):
                    path = save_account(account)
            finally:
                os.umask(previous_umask)

            self.assertEqual(auth_dir.stat().st_mode & 0o777, 0o700)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())["token"], "secret")

    def test_save_account_rejects_explicit_path_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = Path(tmp) / "auth"
            outside = Path(tmp) / "outside.json"
            with patch.object(accountmod, "AUTH_DIR", auth_dir):
                with self.assertRaisesRegex(ValueError, "只能包含"):
                    save_account(
                        Account("claude", "user@example.com", token="secret"),
                        name="../../outside",
                    )
            self.assertFalse(outside.exists())

    def test_default_name_sanitizes_slashes_without_escaping_auth_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth_dir = Path(tmp) / "auth"
            with patch.object(accountmod, "AUTH_DIR", auth_dir):
                path = save_account(Account("claude", "../../nested/user", token="x"))
            self.assertEqual(path.parent, auth_dir.resolve())
            self.assertNotIn("/", path.name)


if __name__ == "__main__":
    unittest.main()
