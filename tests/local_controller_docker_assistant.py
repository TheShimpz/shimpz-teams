"""Real-Docker assertions for Local Assistant install and recovery."""

from __future__ import annotations

import json

from local_controller_docker_fixture import DockerFlow

from action import execution as action_execution
from local.assistant import isolation as local_container_policy


class LocalAssistantLifecycleMixin:
    """Exercise an Assistant's install profile and process recovery through HTTP."""

    def _exercise_assistant(self, flow: DockerFlow) -> None:
        unknown_status, _ = self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/demo_team/assistants",
            {"assistant_id": "unknown-assistant", "source_digest": flow.source_digest},
        )
        self.assertEqual(unknown_status, 404)
        self.assertNotEqual(self._run("image", "inspect", flow.trusted_ref, check=False).returncode, 0)

        installed_status, installed = self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/demo_team/assistants",
            {"assistant_id": "shimpz-cloudflare", "source_digest": flow.source_digest},
        )
        controller_logs = ""
        if installed_status != 200:
            log_result = self._run("logs", flow.controller, check=False)
            controller_logs = (log_result.stdout + log_result.stderr)[-2000:]
        self.assertEqual(installed_status, 200, f"{installed}\n{controller_logs}")
        self.assertTrue(installed["installed"], installed)
        self.assertEqual(self._run("image", "inspect", flow.trusted_ref, check=False).returncode, 0)
        _, catalog = self._api(flow.port, flow.token, "GET", "/v1/assistants")
        self.assertEqual(
            catalog["assistants"],
            [
                {
                    "id": "shimpz-cloudflare",
                    "actions": ["list-dns-records", "list-zones"],
                    "summary": "Shimpz Cloudflare",
                    "title": "Shimpz Cloudflare",
                }
            ],
        )

        flow.assistant_name = self._run(
            "ps",
            "--all",
            "--filter",
            f"label=com.shimpz.local.space-id={flow.space_id}",
            "--filter",
            "label=com.shimpz.local.assistant-id=shimpz-cloudflare",
            "--format",
            "{{.Names}}",
        ).stdout.strip()
        self.assertTrue(flow.assistant_name)
        flow.original_assistant_id = self._run("inspect", "--format", "{{.Id}}", flow.assistant_name).stdout.strip()
        metadata = json.loads(self._run("inspect", flow.assistant_name).stdout)[0]
        host = metadata["HostConfig"]
        self.assertEqual(metadata["Config"]["User"], "10001:10001")
        self.assertTrue(host["ReadonlyRootfs"])
        self.assertEqual(set(host["CapDrop"]), {"ALL"})
        self.assertIn(host.get("CapAdd"), (None, []))
        self.assertEqual(len(host["SecurityOpt"]), 1)
        self.assertIn(host["SecurityOpt"][0], {"no-new-privileges", "no-new-privileges:true"})
        self.assertEqual(host["Memory"], 128 * 1024 * 1024)
        self.assertEqual(host["MemorySwap"], 128 * 1024 * 1024)
        self.assertEqual(host["NanoCpus"], 250_000_000)
        self.assertEqual(host["PidsLimit"], 64)
        self.assertEqual(host["CpusetCpus"], flow.test_cpuset)
        self.assertEqual(host.get("Tmpfs"), local_container_policy.ASSISTANT_TMPFS)
        self.assertEqual(host.get("Ulimits"), local_container_policy.ASSISTANT_ULIMITS)
        self.assertIn(host.get("Sysctls"), (None, {}))
        self.assertEqual(metadata["Mounts"], [])
        self.assertIn(host["PortBindings"], (None, {}))
        networks = metadata["NetworkSettings"]["Networks"]
        self.assertEqual(len(networks), 1)
        flow.network_name = next(iter(networks))
        network_metadata = json.loads(self._run("network", "inspect", flow.network_name).stdout)[0]
        self.assertTrue(network_metadata["Internal"])
        self.assertEqual(network_metadata["Labels"]["com.shimpz.local.space-id"], flow.space_id)
        self.assertEqual(network_metadata["Labels"]["com.shimpz.local.team-name"], "Demo Team")

    def _exercise_assistant_recovery(self, flow: DockerFlow) -> None:
        self._run("kill", flow.assistant_name)
        stopped_state = self._run("inspect", "--format", "{{.State.Status}}", flow.assistant_name).stdout.strip()
        self.assertEqual(stopped_state, "exited")
        recovered_status, recovered = self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/demo_team/assistants",
            {"assistant_id": "shimpz-cloudflare", "source_digest": flow.source_digest},
        )
        self.assertEqual((recovered_status, recovered["installed"]), (200, False))
        restarted_assistant_id = self._run("inspect", "--format", "{{.Id}}", flow.assistant_name).stdout.strip()
        self.assertEqual(restarted_assistant_id, flow.original_assistant_id)
        self.assertEqual(self._run("inspect", flow.original_assistant_id, check=False).returncode, 0)

        _, installed_again = self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/demo_team/assistants",
            {"assistant_id": "shimpz-cloudflare", "source_digest": flow.source_digest},
        )
        self.assertFalse(installed_again["installed"])
        self.assertEqual(
            self._run("inspect", "--format", "{{.Id}}", flow.assistant_name).stdout.strip(),
            restarted_assistant_id,
        )

        _, listed = self._api(flow.port, flow.token, "GET", "/v1/teams/demo_team/assistants")
        self.assertEqual(
            listed["assistants"],
            [{"assistant": "shimpz-cloudflare", "assistant_version": "0.1.0", "status": "running"}],
        )
        account_status, account_inventory = self._api(
            flow.port,
            flow.token,
            "GET",
            "/v1/teams/demo_team/assistant-integrations",
        )
        self.assertEqual(account_status, 200)
        self.assertEqual(len(account_inventory["integrations"]), 1)
        account = account_inventory["integrations"][0]
        self.assertEqual(
            {
                "assistant_id": account["assistant_id"],
                "assistant_name": account["assistant_name"],
                "id": account["id"],
                "provider": account["provider"],
                "name": account["name"],
                "summary": account["summary"],
                "scopes": account["scopes"],
                "status": account["status"],
                "integration": account["integration"],
                "expires_at": account["expires_at"],
            },
            {
                "assistant_id": "shimpz-cloudflare",
                "assistant_name": "Shimpz Cloudflare",
                "id": "cloudflare",
                "provider": "cloudflare",
                "name": "Cloudflare",
                "summary": (
                    "Connect your Cloudflare integration so this Assistant can use only "
                    "its reviewed Cloudflare permissions."
                ),
                "scopes": ["dns.read", "offline_access", "zone.read"],
                "status": "missing",
                "integration": None,
                "expires_at": None,
            },
        )
        account_required, missing_account = self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/demo_team/assistants/shimpz-cloudflare/actions/list-zones",
            {"page": 1, "per_page": 25},
        )
        self.assertEqual(account_required, action_execution.INTEGRATION_PRECONDITION_STATUS)
        self.assertEqual(missing_account["code"], "assistant-integration-unavailable")
        unknown_action, _ = self._api(
            flow.port,
            flow.token,
            "POST",
            "/v1/teams/demo_team/assistants/shimpz-cloudflare/actions/shell",
            {},
        )
        self.assertEqual(unknown_action, 404)
