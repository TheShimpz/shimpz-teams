"""Each Local Assistant's egress policy: Team's private route for its provider calls, never a workload capability."""

import json
import os
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

from local_controller_harness import TestAssistantRegistry

from local import app as local_app
from local import labels as local_labels
from local.assistant import egress as local_egress
from local.assistant import isolation


class LocalAssistantEgressTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.policy_root = Path(self.directory.name) / "policy"
        self.policy_root.mkdir(mode=0o750)
        self.policy_root.chmod(0o750)
        self.controller = object.__new__(local_app.LocalController)
        self.controller.space_id = "local-space"
        self.controller.client = types.SimpleNamespace()
        self.spec = types.SimpleNamespace(
            assistant_id="shimpz-cloudflare",
            allowed_hosts=("api.open-meteo.com", "geocoding-api.open-meteo.com"),
        )
        self.controller.registry = TestAssistantRegistry({self.spec.assistant_id: self.spec})
        self.controller._wire_collaborators()
        for patcher in (
            mock.patch.object(local_egress, "ASSISTANT_EGRESS_POLICY_DIR", self.policy_root),
            mock.patch.object(local_egress, "ASSISTANT_EGRESS_POLICY_GID", os.getgid()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def _write(self) -> str:
        lifecycle = self.controller.assistant_lifecycle
        lifecycle._write_egress_policy("team_1", self.spec, tuple(sorted(self.spec.allowed_hosts)))
        return lifecycle._egress_token("team_1", self.spec.assistant_id, create=False)

    def test_the_policy_is_private_stable_and_exact(self) -> None:
        token = self._write()

        self.assertRegex(token, r"^[0-9a-f]{32}$")
        policy = self.policy_root / f"{token}.json"
        self.assertEqual(json.loads(policy.read_text(encoding="ascii")), sorted(self.spec.allowed_hosts))
        self.assertEqual(policy.stat().st_mode & 0o777, 0o640)
        token_files = list((self.policy_root / ".tokens").glob("*.token"))
        self.assertEqual(len(token_files), 1)
        self.assertEqual(token_files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self._write(), token)
        self.controller.assistant_lifecycle._validate_egress_policy(
            "team_1", self.spec, tuple(sorted(self.spec.allowed_hosts))
        )

    def test_removal_deletes_the_policy_and_its_token_and_is_idempotent(self) -> None:
        token = self._write()
        lifecycle = self.controller.assistant_lifecycle

        lifecycle._remove_egress_policy("team_1", self.spec.assistant_id)
        lifecycle._remove_egress_policy("team_1", self.spec.assistant_id)

        self.assertFalse((self.policy_root / f"{token}.json").exists())
        self.assertEqual(list((self.policy_root / ".tokens").glob("*.token")), [])
        self.assertIsNone(lifecycle._egress_token("team_1", self.spec.assistant_id, create=False))

    def test_policy_tampering_fails_closed(self) -> None:
        for drift in ("content", "mode", "hardlink", "oversize"):
            with self.subTest(drift=drift):
                token = self._write()
                policy = self.policy_root / f"{token}.json"
                if drift == "content":
                    policy.write_text('["evil.example"]', encoding="ascii")
                elif drift == "mode":
                    policy.chmod(0o660)
                elif drift == "hardlink":
                    policy.with_name("policy-hardlink.json").hardlink_to(policy)
                else:
                    policy.write_bytes(b"x" * (local_egress.egress_policy.MAX_POLICY_BYTES + 1))

                with self.assertRaises(local_app.ApiProblem) as caught:
                    self.controller.assistant_lifecycle._validate_egress_policy(
                        "team_1", self.spec, tuple(sorted(self.spec.allowed_hosts))
                    )

                self.assertEqual(caught.exception.code, "egress-policy-drift")
                if drift == "hardlink":
                    policy.with_name("policy-hardlink.json").unlink()

    def test_policy_adapter_maps_unavailable_storage_and_missing_token(self) -> None:
        unavailable = types.SimpleNamespace(
            token=mock.Mock(side_effect=local_egress.egress_policy.EgressPolicyUnavailableError("unavailable")),
            remove=mock.Mock(side_effect=local_egress.egress_policy.EgressPolicyUnavailableError("unavailable")),
        )
        lifecycle = self.controller.assistant_lifecycle
        for operation in (
            lambda: lifecycle._egress_token("team_1", self.spec.assistant_id, create=False, store=unavailable),
            lambda: lifecycle._remove_egress_policy("team_1", self.spec.assistant_id, unavailable),
        ):
            with self.subTest(operation=operation), self.assertRaises(local_app.ApiProblem) as caught:
                operation()
            self.assertEqual(caught.exception.code, "egress-policy-unavailable")

        missing_token = types.SimpleNamespace(token=lambda *_args, **_kwargs: None)
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._write_egress_policy("team_1", self.spec, self.spec.allowed_hosts, missing_token)
        self.assertEqual(caught.exception.code, "egress-policy-unavailable")

    def test_network_validation_and_optional_lookup_fail_closed(self) -> None:
        lifecycle = self.controller.assistant_lifecycle
        invalid = types.SimpleNamespace(reload=mock.Mock(), attrs={})
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._validate_network(invalid, "team_1")
        self.assertEqual(caught.exception.code, "ownership-conflict")

        labels = lifecycle._base_labels("team_1", "team")
        labels[local_labels.TEAM_NAME_LABEL] = ""
        invalid_name = types.SimpleNamespace(
            reload=mock.Mock(),
            attrs={
                "Labels": labels,
                "Name": lifecycle._network_name("team_1"),
                "Driver": "bridge",
                "Internal": True,
                "Attachable": False,
            },
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._validate_network(invalid_name, "team_1")
        self.assertEqual(caught.exception.code, "ownership-conflict")

        fetched = types.SimpleNamespace(attrs={}, reload=mock.Mock())
        self.controller.client.networks = types.SimpleNamespace(get=mock.Mock(return_value=fetched))
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._network("team_1")
        self.assertEqual(caught.exception.code, "ownership-conflict")
        fetched.reload.assert_not_called()

        self.controller.client.networks = types.SimpleNamespace(
            get=mock.Mock(side_effect=local_egress.NotFound("missing"))
        )
        with self.assertRaises(local_app.ApiProblem) as caught:
            lifecycle._network("team_1")
        self.assertEqual(caught.exception.code, "team-not-found")
        self.assertIsNone(lifecycle._network("team_1", required=False))

    def test_a_workload_with_any_proxy_variable_is_refused(self) -> None:
        """An Assistant reaches providers only through Team, so a proxy variable is isolation drift (ADR-0106)."""
        self.assertTrue(isolation.egress_environment_valid({"PYTHONDONTWRITEBYTECODE": "1"}))
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"):
            with self.subTest(name=name):
                self.assertFalse(isolation.egress_environment_valid({name: "http://proxy:8889"}))


if __name__ == "__main__":
    unittest.main()
