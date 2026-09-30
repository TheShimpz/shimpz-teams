from __future__ import annotations

import errno
import os
import stat
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

    def test_principal_and_fence_are_durably_committed_before_the_service_is_called(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            principal_dir = root / "principals"
            provisioner = root / "provisioner"
            provisioner.write_text("b" * 64, encoding="utf-8")
            principal = principal_dir / "team_1.token"
            fence = principal_dir / "team_1.fence"
            real_fsync = os.fsync
            synced: list[str] = []

            def observe(descriptor: int) -> None:
                real_fsync(descriptor)
                synced.append("directory" if stat.S_ISDIR(os.fstat(descriptor).st_mode) else "file")

            def call(path: str, payload: dict, bearer: str) -> dict:
                self.assertEqual(synced, ["file", "directory"] * 2)
                self.assertEqual(principal.read_text(encoding="ascii"), payload["principal_token"])
                self.assertEqual(payload["not_after"], 1000 + postgresql.SERVICE_TIMEOUT_SECONDS)
                self.assertEqual(fence.read_text(encoding="ascii"), str(payload["not_after"]))
                return {"created": True}

            with (
                mock.patch.object(postgresql, "PRINCIPAL_DIR", principal_dir),
                mock.patch.object(postgresql, "PROVISIONER_TOKEN_FILE", provisioner),
                mock.patch.object(postgresql.time, "time", return_value=1000.5),
                mock.patch.object(postgresql.os, "fsync", side_effect=observe),
                mock.patch.object(postgresql, "_call", side_effect=call) as service,
            ):
                self.assertEqual(postgresql.provision_team("team_1"), {"created": True})
            service.assert_called_once()
            self.assertEqual(stat.S_IMODE(principal.stat().st_mode), 0o600)
            self.assertEqual(fence.read_text(encoding="ascii"), "0")

    def test_only_an_answered_provision_settles_its_fence(self) -> None:
        refused = postgresql.PostgreSQLServiceError("postgresql-service /v1/teams/provision failed with status 502")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            principal_dir = root / "principals"
            provisioner = root / "provisioner"
            provisioner.write_text("b" * 64, encoding="utf-8")
            now = [1000.0]
            with (
                mock.patch.object(postgresql, "PRINCIPAL_DIR", principal_dir),
                mock.patch.object(postgresql, "PROVISIONER_TOKEN_FILE", provisioner),
                mock.patch.object(postgresql.time, "time", side_effect=lambda: now[0]),
            ):
                with mock.patch.object(postgresql, "_call", side_effect=refused), self.assertRaises(type(refused)):
                    postgresql.provision_team("team_1")
                self.assertEqual(postgresql._fence("team_1"), 0)

                # An unanswered request may still arrive, so its fence survives until a later answer and beyond.
                with (
                    mock.patch.object(postgresql, "_call", side_effect=OSError("timed out")),
                    self.assertRaises(OSError),
                ):
                    postgresql.provision_team("team_1")
                self.assertEqual(postgresql._fence("team_1"), 1030)
                now[0] = 1010.0
                with mock.patch.object(postgresql, "_call", side_effect=refused), self.assertRaises(type(refused)):
                    postgresql.provision_team("team_1")
                self.assertEqual(postgresql._fence("team_1"), 1030)
                with mock.patch.object(postgresql, "_call", return_value={"created": True}) as call:
                    postgresql.provision_team("team_1")
                self.assertEqual(call.call_args.args[1]["not_after"], 1040)
                self.assertEqual(postgresql._fence("team_1"), 1030)

                postgresql._fence_path("team_1").write_bytes(b"soon")
                with (
                    mock.patch.object(postgresql, "_call") as call,
                    self.assertRaisesRegex(postgresql.PostgreSQLServiceError, "fence is malformed"),
                ):
                    postgresql.provision_team("team_1")
                call.assert_not_called()
                postgresql._fence_path("team_1").unlink()
                with (
                    mock.patch.object(postgresql.private_state, "replace_durably", side_effect=OSError("full")),
                    mock.patch.object(postgresql, "_call") as call,
                    self.assertRaisesRegex(postgresql.PostgreSQLServiceError, "fence could not be persisted"),
                ):
                    postgresql.provision_team("team_1")
                call.assert_not_called()

    def test_the_absence_proof_and_finalization_wait_boundedly_for_the_fence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            principal_dir = Path(directory) / "principals"
            principal_dir.mkdir()
            with (
                mock.patch.object(postgresql, "PRINCIPAL_DIR", principal_dir),
                mock.patch.object(postgresql.time, "time", return_value=1000.0),
                mock.patch.object(postgresql.time, "sleep") as sleep,
            ):
                for fence, delay in ((None, 0.0), (1030, 31.0), (999, 0.0), (9000, 31.0)):
                    if fence is not None:
                        postgresql._fence_path("team_1").write_text(str(fence), encoding="ascii")
                    with self.subTest(fence=fence):
                        self.assertEqual(postgresql._passed_fence("team_1"), fence or 0)
                        sleep.assert_called_with(delay)

    def test_a_partial_principal_write_never_reaches_the_service(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            principal_dir = Path(directory) / "principals"
            real_write = os.write

            def out_of_space(descriptor: int, data: bytes) -> int:
                if len(data) > 12:
                    return real_write(descriptor, bytes(data[:12]))
                raise OSError(errno.ENOSPC, "No space left on device")

            with (
                mock.patch.object(postgresql, "PRINCIPAL_DIR", principal_dir),
                mock.patch.object(postgresql.os, "write", side_effect=out_of_space),
                mock.patch.object(postgresql, "_call") as service,
                self.assertRaisesRegex(postgresql.PostgreSQLServiceError, "could not be persisted"),
            ):
                postgresql.provision_team("team_1")
            service.assert_not_called()
            self.assertEqual(list(principal_dir.iterdir()), [])
            with mock.patch.object(postgresql, "PRINCIPAL_DIR", principal_dir):
                self.assertEqual(len(postgresql._principal("team_1", create=True)), 64)

    def test_drop_falls_back_only_to_the_provisioner_absence_proof(self) -> None:
        refused = postgresql.PostgreSQLServiceError("postgresql-service /v1/teams/drop failed with status 403")
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            principal_dir = root / "principals"
            provisioner = root / "provisioner"
            provisioner.write_text("b" * 64, encoding="utf-8")
            with (
                mock.patch.object(postgresql, "PRINCIPAL_DIR", principal_dir),
                mock.patch.object(postgresql, "PROVISIONER_TOKEN_FILE", provisioner),
            ):
                with mock.patch.object(postgresql, "_call", return_value={"dropped": []}) as call:
                    self.assertEqual(postgresql.drop_team("team_1"), {"dropped": []})
                call.assert_called_once_with("/v1/teams/drop", {"team_id": "team_1", "not_after": 0}, "b" * 64)

                principal_dir.mkdir()
                (principal_dir / "team_1.token").write_text("a" * 64, encoding="utf-8")
                with mock.patch.object(postgresql, "_call", side_effect=[refused, {"dropped": []}]) as call:
                    self.assertEqual(postgresql.drop_team("team_1"), {"dropped": []})
                self.assertEqual([entry.args[2] for entry in call.call_args_list], ["a" * 64, "b" * 64])

                with (
                    mock.patch.object(postgresql, "_call", side_effect=[refused, refused]),
                    self.assertRaises(postgresql.PostgreSQLServiceError),
                ):
                    postgresql.drop_team("team_1")
                with (
                    mock.patch.object(postgresql, "_call", side_effect=OSError("reset")) as call,
                    self.assertRaises(OSError),
                ):
                    postgresql.drop_team("team_1")
                call.assert_called_once()

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
            self.assertEqual(call.call_args.args[1], {"team_id": "team_1", "not_after": 0})
            self.assertFalse(principal.exists())
            self.assertFalse((principal_dir / "team_1.fence").exists())


if __name__ == "__main__":
    unittest.main()
