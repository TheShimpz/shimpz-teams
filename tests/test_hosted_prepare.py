"""Hosted preparation helpers: gVisor, Team accounting, and teardown (ADR-0093)."""

from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness

from prepare import limits
from prepare import service as preparation

# The harness loads the Hosted app with its Docker and state stubs; use the module that app loaded.
hosted_prepare = harness.hosted_lifecycle.hosted_prepare
resources = harness.hosted_resources
state = harness.runtime_state
IMAGE_ID = "sha256:" + "d" * 64


class HostedPrepareTests(unittest.TestCase):
    def setUp(self) -> None:
        state._capacity_reservations.clear()
        self.docker = SimpleNamespace(
            containers=SimpleNamespace(
                get=lambda _host: SimpleNamespace(image=SimpleNamespace(id=IMAGE_ID)),
                list=mock.Mock(return_value=[]),
                create=mock.Mock(),
            ),
            api=SimpleNamespace(),
        )
        patcher = mock.patch.object(state, "_docker", self.docker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_hosted_helper_runs_under_gvisor_with_team_accounting_labels(self) -> None:
        kwargs = hosted_prepare.helper_kwargs(self.docker, team_id="team_1", owner="account_1", key="k1")
        self.assertEqual(kwargs["runtime"], resources.container_spec.RUNTIME)
        self.assertEqual(kwargs["image"], IMAGE_ID)
        self.assertEqual(kwargs["network_mode"], "none")
        self.assertIn("apparmor=docker-default", kwargs["security_opt"])
        self.assertEqual(
            kwargs["labels"],
            {"team.prepare.runtime": "1", "team.prepare.key": "k1", "team.id": "team_1", "team.owner": "account_1"},
        )
        self.assertEqual(kwargs["mem_limit"], limits.HELPER_MEMORY_BYTES)

    def test_each_helper_reserves_its_memory_under_its_own_key(self) -> None:
        with mock.patch.object(resources, "_reserve_capacity") as reserve, hosted_prepare.helper("team_1", "account_1"):
            pass
        key, owner, memory = reserve.call_args.args
        self.assertTrue(key.startswith("prepare:team_1:"))
        self.assertEqual(
            (owner, memory, reserve.call_args.kwargs), ("account_1", limits.HELPER_MEMORY_BYTES, {"team_slot": False})
        )
        residue = SimpleNamespace(labels={"team.prepare.runtime": "1", "team.prepare.key": "old", "team.id": "team_1"})
        self.assertEqual(resources._capacity_key(residue), "prepare:team_1:old")
        self.assertNotEqual(resources._capacity_key(residue), key)

    def test_a_residual_helper_refuses_a_new_one(self) -> None:
        stuck = SimpleNamespace(remove=mock.Mock(side_effect=harness._docker_errors.DockerException("busy")))
        self.docker.containers.list.return_value = [stuck]
        with (
            mock.patch.object(resources, "_reserve_capacity"),
            hosted_prepare.helper("team_1", "account_1") as session,
            self.assertRaises(hosted_prepare.preparation_helper.HelperUnavailableError),
        ):
            session.prepare("image", b"x")
        self.docker.containers.create.assert_not_called()

    def test_teardown_removes_every_helper_and_retries_on_failure(self) -> None:
        stale = SimpleNamespace(remove=mock.Mock())
        gone = SimpleNamespace(remove=mock.Mock(side_effect=harness._docker_errors.NotFound("gone")))
        self.docker.containers.list.return_value = [stale, gone]
        self.assertTrue(hosted_prepare.remove_helpers("team_1"))
        stale.remove.assert_called_once_with(force=True)
        self.docker.containers.list.assert_called_with(
            all=True, filters={"label": ["team.prepare.runtime", "team.id=team_1"]}
        )
        failing = SimpleNamespace(remove=mock.Mock(side_effect=harness._docker_errors.DockerException("busy")))
        self.docker.containers.list.return_value = [failing]
        self.assertFalse(hosted_prepare.remove_helpers("team_1"))
        self.docker.containers.list.side_effect = harness._docker_errors.DockerException("inventory")
        self.assertFalse(hosted_prepare.remove_helpers("team_1"))

    def test_prepare_attachments_uses_the_reserved_helper(self) -> None:
        with mock.patch.object(hosted_prepare.preparation, "prepare_attachments", return_value=()) as prepare:
            self.assertEqual(hosted_prepare.prepare_attachments([], team_id="team_1", owner="account_1"), ())
        files, factory, admission, interrupt = prepare.call_args.args
        self.assertEqual(files, [])
        self.assertTrue(callable(factory))
        self.assertTrue(hasattr(admission, "__enter__"))
        self.assertIsNone(interrupt())

    def test_the_controller_admits_a_bounded_number_of_preparations_before_any_original_is_read(self) -> None:
        bound = hosted_prepare.MAX_CONCURRENT_PREPARATIONS
        release = threading.Event()
        reads: list[str] = []
        guard = threading.Lock()
        all_bound_read = threading.Event()

        def text_file(label: str) -> preparation.StoredFile:
            def read() -> bytes:
                with guard:
                    reads.append(label)
                    if len(reads) == bound:
                        all_bound_read.set()
                release.wait(5)
                return b"plain text"

            return preparation.StoredFile(label * 32, "a.txt", 10, "0" * 64, read)

        turns = [
            threading.Thread(
                target=hosted_prepare.prepare_attachments,
                args=([text_file(chr(ord("a") + index))],),
                kwargs={"team_id": f"team_{index}", "owner": f"account_{index}"},
            )
            for index in range(bound + 1)
        ]
        for turn in turns[:bound]:
            turn.start()
        self.assertTrue(all_bound_read.wait(5))
        turns[bound].start()
        turns[bound].join(0.3)
        # The extra turn waits for a slot without reading its original.
        self.assertEqual(len(reads), bound)
        release.set()
        for turn in turns:
            turn.join(5)
        self.assertEqual(len(reads), bound + 1)

    def test_a_turn_stopped_while_waiting_for_admission_leaves_at_once_and_reads_nothing(self) -> None:
        class StoppedError(Exception):
            pass

        read = mock.Mock(return_value=b"plain text")
        busy = threading.BoundedSemaphore(1)
        busy.acquire()

        def interrupt() -> None:
            raise StoppedError

        with mock.patch.object(hosted_prepare, "_SLOTS", busy), self.assertRaises(StoppedError):
            hosted_prepare.prepare_attachments(
                [preparation.StoredFile("a" * 32, "a.txt", 10, "0" * 64, read)],
                team_id="team_1",
                owner="account_1",
                interrupt=interrupt,
            )
        read.assert_not_called()
        busy.release()

    def test_a_clean_team_starts_its_helper_and_teardown_delegates_removal(self) -> None:
        container = SimpleNamespace(id="helper-1", start=mock.Mock(), remove=mock.Mock())
        self.docker.containers.create.return_value = container
        with (
            mock.patch.object(resources, "_reserve_capacity"),
            mock.patch.object(
                hosted_prepare.preparation_helper.action_execution,
                "rpc_exchange",
                return_value={"type": "text", "text": "ok"},
            ),
            hosted_prepare.helper("team_1", "account_1") as session,
        ):
            self.assertEqual(session.prepare("pdf", b"%PDF"), {"type": "text", "text": "ok"})
        container.start.assert_called_once_with()
        container.remove.assert_called_once_with(force=True)
        with mock.patch.object(hosted_prepare, "remove_helpers", return_value=True) as remove:
            self.assertTrue(harness.hosted_lifecycle._teardown_preparation_helpers("team_1"))
        remove.assert_called_once_with("team_1")


if __name__ == "__main__":
    unittest.main()
