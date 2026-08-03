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
                catalog=lambda: (
                    CatalogPublication("hello-world", "0.2.0", f"sha256:{'9' * 64}"),
                )
            ),
            registry=SimpleNamespace(bindings=lambda: bindings),
            install_publication=lambda *args, **options: calls.append((*args, options)),
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

        def install(team_id: str, *_args, **_options) -> None:
            if team_id == "team_1":
                raise ApiProblem(409, "Team chat is active", code="chat-active")
            updated.append(team_id)

        controller = SimpleNamespace(
            developers=SimpleNamespace(
                catalog=lambda: (
                    CatalogPublication("hello-world", "0.2.0", f"sha256:{'9' * 64}"),
                )
            ),
            registry=SimpleNamespace(bindings=lambda: bindings),
            install_publication=install,
        )

        self.assertTrue(AutomaticAssistantUpdater(controller).run_once())
        self.assertEqual(updated, ["team_2"])
