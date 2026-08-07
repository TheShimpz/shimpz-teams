"""Edge coverage for the Hosted Controller health probe."""

from __future__ import annotations

import json
import runpy
import unittest
from types import SimpleNamespace
from unittest import mock

from hosted import healthcheck


class _Socket:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.error = error
        self.closed = False
        self.timeout = None
        self.request = b""

    def settimeout(self, timeout: int) -> None:
        self.timeout = timeout

    def connect(self, _path: str) -> None:
        if self.error is not None:
            raise self.error

    def sendall(self, request: bytes) -> None:
        self.request = request

    def close(self) -> None:
        self.closed = True


class _Response:
    def __init__(self, payload: object, *, status: int = 200) -> None:
        self.status = status
        self.payload = payload
        self.closed = False

    def begin(self) -> None:
        pass

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()

    def close(self) -> None:
        self.closed = True


def _binding(*, resolution: object = None):
    return SimpleNamespace(
        team_id="team_1",
        assistant_id="assistant_1",
        resolution=resolution if resolution is not None else {"image_reference": "assistant:image"},
    )


class HostedHealthcheckEdgeTests(unittest.TestCase):
    def test_docker_json_sends_request_closes_resources_and_contains_errors(self) -> None:
        client = _Socket()
        response = _Response({"ok": True}, status=201)
        with (
            mock.patch.object(healthcheck.socket, "socket", return_value=client),
            mock.patch.object(healthcheck.http.client, "HTTPResponse", return_value=response),
        ):
            self.assertEqual(healthcheck._docker_json("/info"), (201, {"ok": True}))
        self.assertEqual(client.timeout, 3)
        self.assertIn(b"GET /info HTTP/1.1", client.request)
        self.assertTrue(response.closed)
        self.assertTrue(client.closed)

        failed_client = _Socket(error=OSError("unavailable"))
        with mock.patch.object(healthcheck.socket, "socket", return_value=failed_client):
            self.assertEqual(healthcheck._docker_json("/info"), (0, None))
        self.assertTrue(failed_client.closed)

        malformed_client = _Socket()
        malformed_response = _Response(None)
        malformed_response.read = lambda: b"{"
        with (
            mock.patch.object(healthcheck.socket, "socket", return_value=malformed_client),
            mock.patch.object(healthcheck.http.client, "HTTPResponse", return_value=malformed_response),
        ):
            self.assertEqual(healthcheck._docker_json("/info"), (0, None))
        self.assertTrue(malformed_response.closed)

    def test_daemon_image_and_image_inventory_gates(self) -> None:
        with mock.patch.object(healthcheck, "_docker_json", return_value=(503, None)):
            self.assertFalse(healthcheck.daemon_isolation_ready())
        with (
            mock.patch.object(healthcheck, "_docker_json", return_value=(200, {"Runtimes": {}})),
            mock.patch.object(healthcheck.network_policy, "daemon_isolation_valid", return_value=True) as valid,
        ):
            self.assertTrue(healthcheck.daemon_isolation_ready())
        valid.assert_called_once()

        with mock.patch.object(healthcheck, "_docker_json", return_value=(404, None)):
            self.assertIsNone(healthcheck._image_id("image:tag"))
        with mock.patch.object(healthcheck, "_docker_json", return_value=(200, {"Id": ""})):
            self.assertIsNone(healthcheck._image_id("image:tag"))
        with mock.patch.object(healthcheck, "_docker_json", return_value=(200, {"Id": "sha256:id"})):
            self.assertEqual(healthcheck._image_id("image:tag"), "sha256:id")

        with (
            mock.patch.object(healthcheck, "REQUIRED_IMAGES", ("one", "two")),
            mock.patch.object(healthcheck, "_image_id", side_effect=("id", None)),
        ):
            self.assertFalse(healthcheck.images_ready())

    def test_expected_workload_image_rejects_unreviewed_shapes_and_caches_resolution(self) -> None:
        cache: dict[str, str] = {}
        self.assertIsNone(healthcheck._expected_workload_image({}, cache, {}))
        self.assertIsNone(healthcheck._expected_workload_image({"Config": {"Labels": {}}}, cache, {}))

        assistant = {
            "Config": {
                "Labels": {
                    "team.assistant.runtime": "1",
                    "team.id": "team_1",
                    "team.assistant": "assistant_1",
                }
            }
        }
        self.assertIsNone(healthcheck._expected_workload_image(assistant, cache, {}))
        self.assertIsNone(
            healthcheck._expected_workload_image(
                assistant,
                cache,
                {("team_1", "assistant_1"): _binding(resolution={})},
            )
        )
        with mock.patch.object(healthcheck, "_image_id", return_value=None):
            self.assertIsNone(
                healthcheck._expected_workload_image(
                    assistant,
                    cache,
                    {("team_1", "assistant_1"): _binding()},
                )
            )
        with mock.patch.object(healthcheck, "_image_id", return_value="sha256:assistant") as image_id:
            expected = healthcheck._expected_workload_image(
                assistant,
                cache,
                {("team_1", "assistant_1"): _binding()},
            )
            repeated = healthcheck._expected_workload_image(
                assistant,
                cache,
                {("team_1", "assistant_1"): _binding()},
            )
        self.assertEqual(expected, ("assistant:image", "sha256:assistant", True))
        self.assertEqual(repeated, expected)
        image_id.assert_called_once_with("assistant:image")

    def test_stopped_unbound_exception_requires_exact_nonrestartable_identity(self) -> None:
        labels = {
            "team.assistant.runtime": "1",
            "team.assistant.dynamic": "1",
            "team.id": "team_1",
            "team.assistant": "assistant_1",
        }
        metadata = {"Config": {"Labels": labels}, "HostConfig": {"RestartPolicy": {"Name": "no"}}}
        self.assertTrue(healthcheck._stopped_unbound_assistant(metadata, False, {}))
        self.assertFalse(healthcheck._stopped_unbound_assistant(metadata, True, {}))
        self.assertFalse(healthcheck._stopped_unbound_assistant({}, False, {}))

        for change in (
            {"team.runtime": "1"},
            {"team.assistant.dynamic": "0"},
            {"team.id": ""},
            {"team.assistant": ""},
        ):
            changed = labels | change
            candidate = {"Config": {"Labels": changed}, "HostConfig": metadata["HostConfig"]}
            with self.subTest(change=change):
                self.assertFalse(healthcheck._stopped_unbound_assistant(candidate, False, {}))

        restarting = {"Config": {"Labels": labels}, "HostConfig": {"RestartPolicy": {"Name": "always"}}}
        self.assertFalse(healthcheck._stopped_unbound_assistant(restarting, False, {}))
        self.assertFalse(
            healthcheck._stopped_unbound_assistant(metadata, False, {("team_1", "assistant_1"): _binding()})
        )

    def test_workload_inspection_rejects_malformed_engine_shapes(self) -> None:
        self.assertIsNone(healthcheck._inspect_workloads(["invalid"]))
        self.assertEqual(healthcheck._inspect_workloads([{"Labels": {"other": "1"}}]), ({}, set(), {}, set(), {}))

        base = {"Id": "container", "Labels": {"team.runtime": "1", "team.id": "team_1"}}
        for summary in (
            {"Id": "", "Labels": base["Labels"]},
            {"Id": "container", "Labels": {"team.runtime": "1", "team.id": ""}},
        ):
            with self.subTest(summary=summary):
                self.assertIsNone(healthcheck._inspect_workloads([summary]))

        with mock.patch.object(healthcheck, "_docker_json", return_value=(404, None)):
            self.assertIsNone(healthcheck._inspect_workloads([base]))
        with mock.patch.object(healthcheck, "_docker_json", return_value=(200, {"State": {"Running": "yes"}})):
            self.assertIsNone(healthcheck._inspect_workloads([base]))

    def test_network_member_and_team_network_failures_are_contained(self) -> None:
        self.assertFalse(healthcheck._load_network_members({}, {}))
        inspections: dict[str, dict] = {}
        with mock.patch.object(healthcheck, "_docker_json", return_value=(404, None)):
            self.assertFalse(healthcheck._load_network_members({"Containers": {"other": {}}}, inspections))

        metadata = {"Config": {}}
        with mock.patch.object(healthcheck, "_docker_json", return_value=(200, metadata)):
            self.assertTrue(healthcheck._load_network_members({"Containers": {"other": {}}}, inspections))
        self.assertIs(inspections["other"], metadata)

        with mock.patch.object(healthcheck, "_docker_json", return_value=(404, None)):
            self.assertFalse(healthcheck._team_network_ready("team_1", {}, set(), {}))
        with (
            mock.patch.object(healthcheck, "_docker_json", return_value=(200, {"Containers": {}})),
            mock.patch.object(healthcheck.network_policy, "network_members_valid", return_value=False),
        ):
            self.assertFalse(healthcheck._team_network_ready("team_1", {}, set(), {}))

        workloads = {"workload": ("team_1", frozenset({healthcheck.network_policy.CORE_KIND}), False)}
        with (
            mock.patch.object(healthcheck, "_docker_json", return_value=(200, {"Containers": {}})),
            mock.patch.object(healthcheck, "_load_network_members", return_value=True),
            mock.patch.object(healthcheck.network_policy, "network_members_valid", return_value=True),
            mock.patch.object(healthcheck.network_policy, "workload_endpoint_valid", return_value=False),
        ):
            self.assertFalse(healthcheck._team_network_ready("team_1", {"workload": {}}, set(), workloads))

    def test_network_topology_and_auth_gate_fail_closed(self) -> None:
        with mock.patch.object(healthcheck, "_docker_json", return_value=(503, None)):
            self.assertFalse(healthcheck.network_topology_ready())
        with (
            mock.patch.object(healthcheck, "_docker_json", return_value=(200, [])),
            mock.patch.object(
                healthcheck,
                "_inspect_workloads",
                side_effect=healthcheck.dynamic_assistants.DynamicAssistantError("state"),
            ),
        ):
            self.assertFalse(healthcheck.network_topology_ready())
        with (
            mock.patch.object(healthcheck, "_docker_json", return_value=(200, [])),
            mock.patch.object(healthcheck, "_inspect_workloads", return_value=None),
        ):
            self.assertFalse(healthcheck.network_topology_ready())
        inspected = ({}, {"team_1"}, {"team_1": 2}, set(), {})
        with (
            mock.patch.object(healthcheck, "_docker_json", return_value=(200, [])),
            mock.patch.object(healthcheck, "_inspect_workloads", return_value=inspected),
        ):
            self.assertFalse(healthcheck.network_topology_ready())

        response = mock.MagicMock()
        with mock.patch.object(healthcheck.urllib.request, "urlopen", return_value=response):
            self.assertFalse(healthcheck.auth_gate_ready())
        for code, expected in ((403, True), (401, False)):
            error = healthcheck.urllib.error.HTTPError("url", code, "error", {}, None)
            with mock.patch.object(healthcheck.urllib.request, "urlopen", side_effect=error):
                self.assertEqual(healthcheck.auth_gate_ready(), expected)
        with mock.patch.object(healthcheck.urllib.request, "urlopen", side_effect=OSError("offline")):
            self.assertFalse(healthcheck.auth_gate_ready())

    def test_script_entrypoint_returns_main_status(self) -> None:
        with (
            mock.patch.object(healthcheck, "main", return_value=7),
            self.assertRaises(SystemExit) as stopped,
        ):
            runpy.run_path(healthcheck.__file__, run_name="__main__")
        self.assertIn(stopped.exception.code, (0, 1))


if __name__ == "__main__":
    unittest.main()
