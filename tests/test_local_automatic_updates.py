"""Automatic Local Assistant update selection and race-fencing contracts."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from local.errors import ApiProblemError as ApiProblem
from local.install.automatic import AutomaticAssistantUpdater
from local.install.developers import CatalogPublication, DevelopersError


class AutomaticAssistantUpdaterTests(unittest.TestCase):
    def test_one_catalog_check_updates_every_older_binding_with_its_fence(self) -> None:
        bindings = (
            SimpleNamespace(
                team_id="team_1",
                assistant_id="hello-world",
                binding_digest=f"sha256:{'1' * 64}",
                resolution={"assistant_version": "0.1.0"},
            ),
            SimpleNamespace(
                team_id="team_2",
                assistant_id="hello-world",
                binding_digest=f"sha256:{'2' * 64}",
                resolution={"assistant_version": "0.2.0"},
            ),
        )
        calls: list[tuple[object, ...]] = []
        controller = SimpleNamespace(
            developers=SimpleNamespace(
                catalog=lambda: (CatalogPublication("hello-world", "0.2.0", f"sha256:{'9' * 64}"),)
            ),
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
                )
            ],
        )

    def test_offline_and_busy_teams_defer_without_stopping_other_updates(self) -> None:
        unavailable = SimpleNamespace(
            developers=SimpleNamespace(catalog=lambda: (_ for _ in ()).throw(DevelopersError("offline"))),
            registry=SimpleNamespace(bindings=lambda: ()),
            assistant_lifecycle=SimpleNamespace(sweep_residues=lambda: None),
        )
        self.assertFalse(AutomaticAssistantUpdater(unavailable).run_once())

        bindings = tuple(
            SimpleNamespace(
                team_id=f"team_{index}",
                assistant_id="hello-world",
                binding_digest=f"sha256:{index:064x}",
                resolution={"assistant_version": "0.1.0"},
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
            developers=SimpleNamespace(
                catalog=lambda: (CatalogPublication("hello-world", "0.2.0", f"sha256:{'9' * 64}"),)
            ),
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
            resolution={"assistant_version": "0.1.0"},
        )
        attempts: list[str] = []
        now = [0.0]

        def install(*_args, **_options) -> None:
            attempts.append("update")
            raise ApiProblem(409, "Team chat is active", code="chat-active")

        controller = SimpleNamespace(
            developers=SimpleNamespace(
                catalog=lambda: (CatalogPublication("hello-world", "0.2.0", f"sha256:{'9' * 64}"),)
            ),
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
