"""Automatic Local Assistant update selection and race-fencing contracts."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from install import bindings
from local.errors import ApiProblemError as ApiProblem
from local.install import automatic
from local.install.automatic import AutomaticAssistantUpdater
from local.install.developers import DevelopersError, PublicationNotInstallableError


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

    def test_no_candidate_is_distinct_from_developers_unavailability(self) -> None:
        binding = SimpleNamespace(
            team_id="team_1",
            assistant_id="hello-world",
            binding_digest=f"sha256:{'1' * 64}",
            resolution={"assistant_version": "0.1.0", "source_digest": f"sha256:{'a' * 64}"},
        )
        audits: list[tuple[str, str, str, str]] = []
        errors = iter((PublicationNotInstallableError("absent"), DevelopersError("offline")))
        controller = SimpleNamespace(
            developers=SimpleNamespace(latest=lambda _digest: (_ for _ in ()).throw(next(errors))),
            registry=SimpleNamespace(bindings=lambda: (binding,)),
            assistant_lifecycle=SimpleNamespace(sweep_residues=lambda: None),
        )
        updater = AutomaticAssistantUpdater(
            controller,
            interval_seconds=1,
            clock=lambda: float(len(audits)),
            record=lambda *event: audits.append(event),
        )

        self.assertTrue(updater.run_once())
        self.assertTrue(updater.run_once())
        self.assertEqual(
            audits,
            [
                ("team_1", "hello-world", "ok", "deferred:no-candidate"),
                ("team_1", "hello-world", "error", "deferred:developers-unavailable"),
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

    def test_thread_lifecycle_and_unavailable_binding_store_are_bounded(self) -> None:
        controller = SimpleNamespace(
            registry=SimpleNamespace(bindings=mock.Mock(side_effect=bindings.DynamicAssistantError("unavailable"))),
            assistant_lifecycle=SimpleNamespace(sweep_residues=mock.Mock()),
        )
        records: list[tuple[object, ...]] = []
        updater = AutomaticAssistantUpdater(controller, record=lambda *event: records.append(event))
        self.assertFalse(updater.run_once())
        self.assertEqual(records, [(None, None, "error", "cycle:bindings-unavailable")])

        thread = mock.Mock()
        thread.is_alive.return_value = True
        with mock.patch.object(automatic.threading, "Thread", return_value=thread):
            updater.start()
        thread.start.assert_called_once_with()
        with self.assertRaisesRegex(RuntimeError, "already running"):
            updater.start()
        updater.close()
        thread.join.assert_called_once_with(timeout=30)

        idle = AutomaticAssistantUpdater(controller)
        idle.close()
        stopped_thread = mock.Mock()
        stopped_thread.is_alive.return_value = False
        idle._thread = stopped_thread
        idle.close()
        stopped_thread.join.assert_called_once_with(timeout=30)

    def test_invalid_binding_identity_and_current_candidate_are_not_installed(self) -> None:
        invalid = SimpleNamespace(
            team_id="team_1",
            assistant_id="hello-world",
            binding_digest=f"sha256:{'1' * 64}",
            resolution={"assistant_version": "0.1.0", "source_digest": None},
        )
        mismatch = SimpleNamespace(
            team_id="team_2",
            assistant_id="hello-world",
            binding_digest=f"sha256:{'2' * 64}",
            resolution={"assistant_version": "0.1.0", "source_digest": f"sha256:{'a' * 64}"},
        )
        current = SimpleNamespace(
            team_id="team_3",
            assistant_id="hello-world",
            binding_digest=f"sha256:{'3' * 64}",
            resolution={"assistant_version": "0.2.0", "source_digest": f"sha256:{'b' * 64}"},
        )

        def latest(digest: str) -> dict[str, str]:
            if digest.endswith("a" * 64):
                return {**_candidate(), "assistant_id": "other"}
            return _candidate("0.2.0")

        records: list[tuple[object, ...]] = []
        controller = SimpleNamespace(
            developers=SimpleNamespace(latest=latest),
            registry=SimpleNamespace(bindings=lambda: (invalid, mismatch, current)),
            install_publication=mock.Mock(),
            assistant_lifecycle=SimpleNamespace(sweep_residues=lambda: None),
        )
        updater = AutomaticAssistantUpdater(controller, record=lambda *event: records.append(event))
        self.assertTrue(updater.run_once())
        controller.install_publication.assert_not_called()
        self.assertIn(("team_1", "hello-world", "error", "binding:invalid"), records)
        self.assertIn(("team_2", "hello-world", "error", "candidate:identity-mismatch"), records)

    def test_worker_loop_backs_off_and_recovers_from_unexpected_failure(self) -> None:
        controller = SimpleNamespace()
        records: list[tuple[object, ...]] = []
        updater = AutomaticAssistantUpdater(
            controller,
            interval_seconds=10,
            jitter=lambda _maximum: 3,
            record=lambda *event: records.append(event),
        )
        updater._stop = mock.Mock()
        updater._stop.wait.side_effect = (False, False, False, True)
        updater.run_once = mock.Mock(side_effect=(True, False, RuntimeError("unexpected")))
        updater._run()
        self.assertEqual([call.args[0] for call in updater._stop.wait.call_args_list], [0, 13, 26, 52])
        self.assertEqual(records, [(None, None, "error", "thread:runtime-unavailable")])

        updater._record = mock.Mock(side_effect=RuntimeError("audit unavailable"))
        updater._record_result(None, None, "error", "detail")
