"""Real-Docker assertions for Local Assistant egress attachment recovery."""

from __future__ import annotations

import json

from local_controller_docker_fixture import DockerFlow


class LocalEgressRecoveryMixin:
    """Exercise daemon-side attachment loss through the production HTTP path."""

    def _exercise_egress_attachment_recovery(self, flow: DockerFlow) -> None:
        controller_started_at = self._run(
            "inspect",
            "--format",
            "{{.State.StartedAt}}",
            flow.controller,
        ).stdout.strip()
        self._run("network", "disconnect", flow.network_name, flow.egress_proxy)
        detached_networks = json.loads(self._run("inspect", flow.egress_proxy).stdout)[0]["NetworkSettings"]["Networks"]
        self.assertNotIn(flow.network_name, detached_networks)

        inventory_status, inventory = self._api(
            flow.port,
            flow.token,
            "GET",
            "/v1/teams/demo_team/assistants",
        )
        self.assertEqual(inventory_status, 200, inventory)
        self.assertEqual(inventory["assistants"], [{"assistant": "shimpz-cloudflare", "status": "running"}])

        repaired_networks = json.loads(self._run("inspect", flow.egress_proxy).stdout)[0]["NetworkSettings"]["Networks"]
        self.assertIn(flow.network_name, repaired_networks)
        self.assertIn("shimpz-assistant-egress", repaired_networks[flow.network_name]["Aliases"])
        self.assertEqual(
            self._run("inspect", "--format", "{{.State.StartedAt}}", flow.controller).stdout.strip(),
            controller_started_at,
        )
