from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from egress import policy as egress_policy


class SharedEgressPolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name) / "policies"
        self.root.mkdir(mode=0o770)
        self.root.chmod(0o770)
        self.store = egress_policy.EgressPolicyStore(self.root, os.getgid(), "localhost")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_hosted_and_local_stores_make_the_same_drift_decision(self) -> None:
        hosts = ("api.open-meteo.com", "geocoding-api.open-meteo.com")
        decisions: list[type[Exception]] = []

        with tempfile.TemporaryDirectory() as directory:
            for name, no_proxy in (
                ("hosted", "localhost,127.0.0.1,::1,postgres,.team"),
                ("local", "127.0.0.1,localhost"),
            ):
                root = Path(directory) / name
                root.mkdir(mode=0o770)
                root.chmod(0o770)
                store = egress_policy.EgressPolicyStore(root, os.getgid(), no_proxy)
                token = store.token("space\0team_1\0assistant", create=True)
                self.assertIsNotNone(token)
                assert token is not None
                store.write(token, hosts)
                (root / f"{token}.json").write_text('["evil.example"]', encoding="ascii")

                with self.assertRaises(egress_policy.EgressPolicyError) as caught:
                    store.validate("space\0team_1\0assistant", hosts)
                decisions.append(type(caught.exception))

        self.assertEqual(decisions, [egress_policy.EgressPolicyDriftError] * 2)

    def test_atomic_and_exact_file_io_fail_closed(self) -> None:
        target = self.root / "target"
        with mock.patch.object(os, "write", return_value=0), self.assertRaises(OSError):
            egress_policy._atomic_write(target, b"content", mode=0o600)
        self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

        target.write_bytes(b"safe")
        target.chmod(0o600)
        with (
            mock.patch.object(os, "read", return_value=b""),
            self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "changed while"),
        ):
            egress_policy._read_exact_private_file(
                target,
                mode=0o600,
                group=None,
                minimum_bytes=4,
                maximum_bytes=4,
            )
        with (
            mock.patch.object(os, "read", side_effect=[b"safe", b"x"]),
            self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "changed while"),
        ):
            egress_policy._read_exact_private_file(
                target,
                mode=0o600,
                group=None,
                minimum_bytes=4,
                maximum_bytes=4,
            )
        with (
            mock.patch.object(os, "open", side_effect=OSError("denied")),
            self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "unavailable"),
        ):
            egress_policy._read_exact_private_file(
                target,
                mode=0o600,
                group=None,
                minimum_bytes=4,
                maximum_bytes=4,
            )
        target.chmod(0o640)
        with self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "metadata drifted"):
            egress_policy._read_exact_private_file(
                target,
                mode=0o600,
                group=None,
                minimum_bytes=4,
                maximum_bytes=4,
            )

    def test_store_and_identity_metadata_are_strict(self) -> None:
        missing = egress_policy.EgressPolicyStore(self.root / "missing", os.getgid(), "localhost")
        with self.assertRaises(egress_policy.EgressPolicyUnavailableError):
            missing.token("identity", create=False)

        self.root.chmod(0o700)
        with self.assertRaises(egress_policy.EgressPolicyDriftError):
            self.store.token("identity", create=False)
        self.root.chmod(0o770)
        with self.assertRaises(egress_policy.EgressPolicyDriftError):
            self.store.token("", create=False)

        with (
            mock.patch.object(Path, "mkdir", side_effect=OSError("denied")),
            self.assertRaises(egress_policy.EgressPolicyUnavailableError),
        ):
            self.store.token("identity", create=False)

        token_dir = self.root / ".tokens"
        token_dir.mkdir(mode=0o700, exist_ok=True)
        token_dir.chmod(stat.S_IMODE(token_dir.stat().st_mode) | stat.S_IRGRP)
        with self.assertRaises(egress_policy.EgressPolicyDriftError):
            self.store.token("identity", create=False)

    def test_token_content_creation_and_metadata_errors_are_mapped(self) -> None:
        self.assertIsNone(self.store.token("missing", create=False))
        path = self.store._token_path("identity")
        path.write_bytes(b"\xff" + (b"0" * 31) + b"\n")
        path.chmod(0o600)
        with self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "not canonical"):
            self.store.token("identity", create=False)

        path.write_bytes(b"g" + (b"0" * 31) + b"\n")
        with self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "not canonical"):
            self.store.token("identity", create=False)
        path.unlink()

        original_stat = Path.stat

        def fail_token_stat(current: Path, *args: object, **kwargs: object) -> os.stat_result:
            if current.suffix == ".token":
                raise OSError("denied")
            return original_stat(current, *args, **kwargs)

        with (
            mock.patch.object(Path, "stat", autospec=True, side_effect=fail_token_stat),
            self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "metadata is unavailable"),
        ):
            self.store.token("identity", create=False)
        with (
            mock.patch.object(egress_policy, "_atomic_write", side_effect=OSError("denied")),
            self.assertRaises(egress_policy.EgressPolicyUnavailableError),
        ):
            self.store.token("identity", create=True)

    def test_proxy_and_policy_inputs_must_be_canonical(self) -> None:
        with self.assertRaises(egress_policy.EgressPolicyDriftError):
            self.store.proxy_environment("invalid")
        with self.assertRaises(egress_policy.EgressPolicyDriftError):
            self.store._canonical_hosts(("https://example.com",))
        with self.assertRaises(egress_policy.EgressPolicyDriftError):
            self.store._canonical_hosts(("EXAMPLE.com",))
        with self.assertRaises(egress_policy.EgressPolicyDriftError):
            self.store._canonical_hosts(("b.com", "a.com"))
        with self.assertRaises(egress_policy.EgressPolicyDriftError):
            self.store.write("invalid", ("example.com",))

    def test_policy_write_and_admission_fail_closed_on_storage_drift(self) -> None:
        token = self.store.token("identity", create=True)
        assert token is not None
        hosts = ("example.com",)
        with (
            mock.patch.object(egress_policy, "_atomic_write", side_effect=OSError("denied")),
            self.assertRaises(egress_policy.EgressPolicyUnavailableError),
        ):
            self.store.write(token, hosts)

        atomic_write = egress_policy._atomic_write

        def write_with_wrong_mode(path: Path, content: bytes, *, mode: int, group: int | None = None) -> None:
            atomic_write(path, content, mode=mode, group=group)
            path.chmod(0o600)

        with (
            mock.patch.object(egress_policy, "_atomic_write", side_effect=write_with_wrong_mode),
            self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "metadata drifted"),
        ):
            self.store.write(token, hosts)

        self.assertIsNone(self.store.admitted("another-identity"))
        policy_path = self.root / f"{token}.json"
        policy_path.write_bytes(b"not-json")
        policy_path.chmod(0o640)
        with self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "content is invalid"):
            self.store.admitted("identity")
        policy_path.write_bytes(b'["example.com"]\n')
        with self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "not canonical"):
            self.store.admitted("identity")

        self.store.write(token, hosts)
        admitted = self.store.admitted("identity")
        with self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "hosts drifted"):
            self.store.validate_admitted(admitted, ("other.com",))

    def test_remove_handles_absence_drift_and_io_failures(self) -> None:
        self.store.remove("missing")
        token = self.store.token("identity", create=True)
        assert token is not None
        self.store.remove("identity")
        self.assertIsNone(self.store.token("identity", create=False))

        token = self.store.token("identity", create=True)
        assert token is not None
        self.store.write(token, ("example.com",))
        policy_path = self.root / f"{token}.json"
        policy_path.chmod(0o600)
        with self.assertRaisesRegex(egress_policy.EgressPolicyDriftError, "metadata drifted"):
            self.store.remove("identity")

        policy_path.chmod(0o640)
        with (
            mock.patch.object(Path, "unlink", side_effect=OSError("denied")),
            self.assertRaises(egress_policy.EgressPolicyUnavailableError),
        ):
            self.store.remove("identity")

        token_path = self.store._token_path("identity")
        original_stat = Path.stat

        def fail_token_stat(current: Path, *args: object, **kwargs: object) -> os.stat_result:
            if current == token_path:
                raise OSError("denied")
            return original_stat(current, *args, **kwargs)

        with (
            mock.patch.object(Path, "stat", autospec=True, side_effect=fail_token_stat),
            self.assertRaisesRegex(egress_policy.EgressPolicyUnavailableError, "metadata is unavailable"),
        ):
            self.store.remove("identity")


if __name__ == "__main__":
    unittest.main()
