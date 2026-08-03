"""Automatic Local Assistant update selection and race-fencing contracts."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from local.errors import ApiProblemError as ApiProblem
from local.install.automatic import AutomaticAssistantUpdater
from local.install.developers import DevelopersError


def _candidate(version: str = "0.2.0") -> dict[str, str]:
    return {
        "assistant_id": "hello-world",
        "assistant_version": version,
        "source_digest": f"sha256:{'9' * 64}",
    }


class AutomaticAssistantUpdaterTests(unittest.TestCase):
    def test_one_candidate_per_installed_digest_updates_every_older_binding_with_its_fence(self) -> None:
        bindings = (
            SimpleNamespace(
                team_id="team_1",
                assistant_id="hello-world",
                binding_digest=f"sha256:{'1' * 64}",
                resolution={"assistant_version": "0.1.0", "source_digest": f"sha256:{'a' * 64}"},
            ),
            SimpleNamespace(
                team_id="team_2",
                assistant_id="hello-world",
                binding_digest=f"sha256:{'2' * 64}",
                resolution={"assistant_version": "0.1.0", "source_digest": f"sha256:{'a' * 64}"},
            ),
        )
        calls: list[tuple[object, ...]] = []
        candidate_calls: list[str] = []
        controller = SimpleNamespace(
            developers=SimpleNamespace(latest=lambda digest: candidate_calls.append(digest) or _candidate()),
            registry=SimpleNamespace(bindings=lambda: bindings),
            install_publication=lambda *args, **options: calls.append((*args, options)),
            assistant_lifecycle=SimpleNamespace(sweep_residues=lambda: None),
        )

        self.assertTrue(AutomaticAssistantUpdater(controller).run_once())
        self.assertEqual(
            calls,
            [
                (
                    "team_1",
                    "hello-world",
                    f"sha256:{'9' * 64}",
                    {"expected_binding_digest": f"sha256:{'1' * 64}"},
                ),
                (
                    "team_2",
                    "hello-world",
                    f"sha256:{'9' * 64}",
                    {"expected_binding_digest": f"sha256:{'2' * 64}"},
                ),
            ],
        )
        self.assertEqual(candidate_calls, [f"sha256:{'a' * 64}"])

    def test_offline_and_busy_teams_defer_without_stopping_other_updates(self) -> None:
        unavailable_binding = SimpleNamespace(
            team_id="team_offline",
            assistant_id="hello-world",
            binding_digest=f"sha256:{'3' * 64}",
            resolution={"assistant_version": "0.1.0", "source_digest": f"sha256:{'c' * 64}"},
        )
        unavailable = SimpleNamespace(
            developers=SimpleNamespace(latest=lambda _digest: (_ for _ in ()).throw(DevelopersError("offline"))),
            registry=SimpleNamespace(bindings=lambda: (unavailable_binding,)),
            assistant_lifecycle=SimpleNamespace(sweep_residues=lambda: None),
        )
        self.assertTrue(AutomaticAssistantUpdater(unavailable).run_once())

        bindings = tuple(
            SimpleNamespace(
                team_id=f"team_{index}",
                assistant_id="hello-world",
                binding_digest=f"sha256:{index:064x}",
                resolution={"assistant_version": "0.1.0", "source_digest": f"sha256:{'a' * 64}"},
            )
            for index in (1, 2)
        )
        updated: list[str] = []
        audits: list[tuple[str, str, str, str]] = []

        def install(team_id: str, *_args, **_options) -> None:
            if team_id == "team_1":
                raise ApiProblem(409, "Team chat is active", code="chat-active")
            updated.append(team_id)

        controller = SimpleNamespace(
            developers=SimpleNamespace(latest=lambda _digest: _candidate()),
            registry=SimpleNamespace(bindings=lambda: bindings),
            install_publication=install,
            assistant_lifecycle=SimpleNamespace(sweep_residues=lambda: None),
        )

        self.assertTrue(AutomaticAssistantUpdater(controller, record=lambda *event: audits.append(event)).run_once())
        self.assertEqual(updated, ["team_2"])
        self.assertEqual(
            audits,
            [
                ("team_1", "hello-world", "error", "deferred:chat-active"),
                ("team_2", "hello-world", "ok", "updated:0.1.0:0.2.0"),
            ],
        )

    def test_failing_binding_uses_independent_bounded_backoff(self) -> None:
        binding = SimpleNamespace(
            team_id="team_1",
            assistant_id="hello-world",
            binding_digest=f"sha256:{'1' * 64}",
            resolution={"assistant_version": "0.1.0", "source_digest": f"sha256:{'a' * 64}"},
        )
        attempts: list[str] = []
        now = [0.0]

        def install(*_args, **_options) -> None:
            attempts.append("update")
            raise ApiProblem(409, "Team chat is active", code="chat-active")

        controller = SimpleNamespace(
            developers=SimpleNamespace(latest=lambda _digest: _candidate()),
            registry=SimpleNamespace(bindings=lambda: (binding,)),
            install_publication=install,
            assistant_lifecycle=SimpleNamespace(sweep_residues=lambda: None),
        )
        updater = AutomaticAssistantUpdater(
            controller,
            interval_seconds=300,
            clock=lambda: now[0],
        )

        updater.run_once()
        updater.run_once()
        self.assertEqual(attempts, ["update"])

        now[0] = 300
        updater.run_once()
        self.assertEqual(attempts, ["update", "update"])
