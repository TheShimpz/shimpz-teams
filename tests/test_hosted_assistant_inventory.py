from __future__ import annotations

import types
import unittest
from unittest import mock

from hosted_assistant_fixture import (
    HOSTED_SPEC,
    assistant_lifecycle,
    hosted_assistants,
    hosted_resources,
    runtime_state,
)


class HostedAssistantInventoryTests(unittest.TestCase):
    def test_active_assistants_inspect_network_members_once_per_listing(self) -> None:
        first_id = "shimpz-cloudflare"
        second_id = "second-assistant"
        spec = HOSTED_SPEC
        candidate_ids = (first_id, second_id)
        candidates = [
            types.SimpleNamespace(
                id=f"container-{assistant_id}",
                labels={"team.assistant": assistant_id},
                status="running",
                reload=mock.Mock(),
            )
            for assistant_id in candidate_ids
        ]
        members = {
            member_id: types.SimpleNamespace(id=member_id, name=member_id, attrs={}, reload=mock.Mock())
            for member_id in ("member-one", "member-two")
        }
        network = types.SimpleNamespace(
            id="core-network-id",
            attrs={"Containers": dict.fromkeys(members)},
            reload=mock.Mock(),
        )

        admitted_candidates = []

        def installed(_team_id: str, assistant_id: str, inspect_memo, candidate, *_args):
            admitted_candidates.append(candidate)
            hosted_resources._network_container_metadata(network, inspect_memo)
            return assistant_id, spec.contract, candidate

        engine = types.SimpleNamespace(
            containers=types.SimpleNamespace(get=lambda member_id: members[member_id]),
        )
        with (
            mock.patch.object(runtime_state, "_docker", engine),
            mock.patch.object(assistant_lifecycle, "_team_assistant_containers", return_value=candidates),
            mock.patch.object(
                assistant_lifecycle,
                "_resolve_team_assistant",
                side_effect=lambda _team_id, assistant_id, *_args: (
                    assistant_id,
                    spec,
                ),
            ),
            mock.patch.object(
                assistant_lifecycle,
                "_dynamic_binding_snapshot",
                return_value={
                    assistant_id: types.SimpleNamespace(resolution={"assistant_version": "0.4.1"})
                    for assistant_id in candidate_ids
                },
            ),
            mock.patch.object(hosted_assistants, "_installed_assistant", side_effect=installed),
        ):
            active = hosted_assistants._active_team_assistants("team_1")

        self.assertEqual(tuple(item.assistant_id for item in active), (second_id, first_id))
        self.assertEqual(admitted_candidates, candidates)
        network.reload.assert_called_once_with()
        for member in members.values():
            member.reload.assert_not_called()


if __name__ == "__main__":
    unittest.main()
