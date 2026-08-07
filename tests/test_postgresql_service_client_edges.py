from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hosted.team import postgresql


class PostgreSQLServiceClientEdgeTests(unittest.TestCase):
    def test_non_object_response_and_invalid_team_id_fail_closed(self) -> None:
        response = mock.Mock(status=200)
        response.read.return_value = b"[]"
        connection = mock.Mock()
        connection.getresponse.return_value = response
        with (
            mock.patch.object(postgresql.http.client, "HTTPConnection", return_value=connection),
            self.assertRaises(postgresql.PostgreSQLServiceError),
        ):
            postgresql._call("/v1/teams/provision", {}, "a" * 64)
        connection.close.assert_called_once_with()

        with self.assertRaises(postgresql.PostgreSQLServiceError):
            postgresql._principal_path("../escape")

    def test_principal_is_persisted_validated_and_permissioned(self) -> None:
        with (
            tempfile.TemporaryDirectory() as directory,
            mock.patch.object(
                postgresql,
                "PRINCIPAL_DIR",
                Path(directory) / "principals",
            ),
        ):
            with self.assertRaises(postgresql.PostgreSQLServiceError):
                postgresql._principal("team_1", create=False)

            with mock.patch.object(postgresql.secrets, "token_hex", return_value="a" * 64):
                self.assertEqual(postgresql._principal("team_1", create=True), "a" * 64)
            path = postgresql._principal_path("team_1")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(postgresql._principal("team_1", create=False), "a" * 64)

            path.write_text("invalid", encoding="utf-8")
            with self.assertRaises(postgresql.PostgreSQLServiceError):
                postgresql._principal("team_1", create=True)

    def test_public_operations_use_the_correct_authority_and_finalize_local_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            principal_dir = root / "principals"
            principal_dir.mkdir()
            principal = principal_dir / "team_1.token"
            principal.write_text("a" * 64, encoding="utf-8")
            provisioner = root / "provisioner"
            provisioner.write_text("b" * 64, encoding="utf-8")
            with (
                mock.patch.object(postgresql, "PRINCIPAL_DIR", principal_dir),
                mock.patch.object(postgresql, "PROVISIONER_TOKEN_FILE", provisioner),
                mock.patch.object(postgresql, "_call", return_value={"ok": True}) as call,
            ):
                self.assertEqual(postgresql.provision_team("team_1"), {"ok": True})
                self.assertEqual(postgresql.drop_team("team_1"), {"ok": True})
                self.assertEqual(postgresql.finalize_team_drop("team_1"), {"ok": True})
            self.assertEqual(call.call_count, 3)
            self.assertFalse(principal.exists())


if __name__ == "__main__":
    unittest.main()
