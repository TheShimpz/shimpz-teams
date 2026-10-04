"""Destroying a Team or resetting the Space removes every Routine run's journal generation and state."""

from __future__ import annotations

import dataclasses
import datetime
import errno
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import routine_fixture

from action import challenges as action_challenges
from action import journal as action_journal
from local.errors import ApiProblemError
from local.routine import card as routine_card
from local.routine import diagnostics as routine_diagnostics
from local.routine import lifecycle as routine_lifecycle
from local.routine import lineage as routine_lineage
from local.routine import store as routine_store
from local.validation import routine_thread_id
from routine import record

KEY = "e" * 64
NETWORK = "a" * 64
NINE = int(datetime.datetime(2026, 10, 1, 9, tzinfo=datetime.UTC).timestamp())


def put(store: routine_store.RoutineStore, team_id: str, state: record.TeamRoutines) -> None:
    """Replace a Team's Routine state through the store's only write path."""
    store.update(team_id, lambda _before: (state, None))


def routine(routine_id: str) -> record.Routine:
    value = routine_fixture.granted(
        record.Routine(
            routine_id=routine_id,
            name="Daily DNS check",
            quote="Every day at 9, check the DNS.",
            plan=routine_fixture.plan_document(),
            schedule={"kind": "daily", "time": "09:00"},
            timezone="UTC",
            assistants=(("dns", "sha256:" + "c" * 64),),
            anchor=NINE - 86_400,
            next_run_at=0,
        )
    )
    return dataclasses.replace(value, next_run_at=record.next_after(value, value.anchor))


def two_runs() -> tuple[record.TeamRoutines, str]:
    """One run that bound its generation and froze for a person, and one that has not started a segment yet."""
    state = record.add_routine(record.add_routine(record.TeamRoutines(), routine("a" * 32)), routine("b" * 32))
    state, bound = record.claim(state, NINE, KEY)
    lease = record.lease_of(bound.lease_token, KEY)
    state = record.bind_generation(state, bound.run.run_id, lease, NINE, NETWORK)
    # A frozen run holds no execution slot, so the Team may lease its other due Routine.
    state = record.freeze(state, bound.run.run_id, lease, NINE, "human", "dns", "replace-dns-record")
    state, fresh = record.claim(state, NINE, KEY)
    assert fresh is not None
    return state, bound.run.run_id


class RoutineLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        # The Routine state and key volumes, as the Local graph mounts them.
        for volume in ("state", "key"):
            (root / volume).mkdir(mode=0o700)
        self.events: list[object] = []
        self.subject = SimpleNamespace(
            space_id="local-space",
            routine_store=routine_store.RoutineStore(root / "state", root / "key" / "aes256.key"),
            # The Local layout: the diagnostic family shares the Routine state and key volumes in its own directories.
            routine_diagnostics=routine_diagnostics.DiagnosticStore(
                root / "state" / "diagnostics", root / "key" / "diagnostics.key"
            ),
            action_state=SimpleNamespace(purge=lambda generation: self.events.append(("purge", generation))),
            routine_human_challenges=action_challenges.HumanChallengeStore(),
            routine_lineage=routine_lineage.LineageBook(),
            routine_cards=routine_card.CardBook(),
        )

    def test_a_teams_routine_threads_generations_and_state_are_deleted(self):
        state, run_id = two_runs()
        put(self.subject.routine_store, "team_1", state)
        self.subject.routine_store.put_continuation("team_1", run_id, b"continuation")
        routine_lifecycle.delete_team_routines(self.subject, "team_1")
        self.assertEqual(
            self.events,
            [("purge", f"{NETWORK}:routine:{run_id}")],
        )
        self.assertEqual(
            (self.subject.routine_store.teams(), self.subject.routine_store.continuations("team_1")), ((), ())
        )
        # An absent Team is already clean.
        routine_lifecycle.delete_team_routines(self.subject, "team_1")

    def record_diagnostic(self, team_id: str, run_id: str) -> None:
        self.subject.routine_diagnostics.record(
            team_id,
            NETWORK,
            routine_diagnostics.Diagnostic(
                routine_id="a" * 32,
                run_id=run_id,
                operation_id="6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
                attempt=1,
                assistant_id="dns",
                action="replace-dns-record",
                recorded_at=NINE,
                condition="timeout",
            ),
            (),
        )

    def test_diagnostics_share_the_routine_volumes_and_leave_with_their_team(self):
        state, run_id = two_runs()
        for team in ("team_1", "team_2"):
            put(self.subject.routine_store, team, state)
            self.record_diagnostic(team, run_id)
        self.subject.routine_store.put_continuation("team_2", run_id, b"continuation")
        diagnostics = self.subject.routine_diagnostics
        self.assertEqual(stat.S_IMODE(diagnostics.root.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(diagnostics.key_path.stat().st_mode), 0o600)
        # Neither store mistakes the other's entries for its own.
        self.assertEqual(set(self.subject.routine_store.teams()), {"team_1", "team_2"})
        self.assertTrue(self.subject.routine_store.key_path.exists())
        self.assertEqual(len(diagnostics.read("team_2", NETWORK, run_id, NINE)), 1)
        routine_lifecycle.delete_team_routines(self.subject, "team_1")
        self.assertFalse(diagnostics._team_dir("team_1").exists())
        self.assertEqual(self.subject.routine_store.teams(), ("team_2",))
        self.assertEqual(len(diagnostics.read("team_2", NETWORK, run_id, NINE)), 1)
        self.assertEqual(self.subject.routine_store.continuation("team_2", run_id), b"continuation")
        routine_lifecycle.delete_all_routines(self.subject)
        self.assertEqual(self.subject.routine_store.teams(), ())
        self.assertEqual(list(diagnostics.root.iterdir()), [])
        self.assertFalse(self.subject.routine_store.key_path.exists())
        self.assertFalse(diagnostics.key_path.exists())

    def test_a_reset_fails_closed_when_a_routine_volume_is_not_writable(self):
        # An unmounted volume leaves the image's read-only directory, where even an absent key cannot be unlinked.
        unwritable = OSError(errno.EROFS, "Read-only file system")
        original = Path.unlink
        for owner in ("routine_store", "routine_diagnostics"):
            key_path = getattr(self.subject, owner).key_path

            def unlink(path, *args, key_path=key_path, **kwargs):
                if path == key_path:
                    raise unwritable
                return original(path, *args, **kwargs)

            with self.subTest(owner=owner), mock.patch.object(Path, "unlink", unlink):
                with self.assertRaises(ApiProblemError) as caught:
                    routine_lifecycle.delete_all_routines(self.subject)
                self.assertEqual((caught.exception.status, caught.exception.code), (503, "routine-state-unavailable"))

    def test_queued_discards_are_cleaned_before_state_and_kept_when_cleanup_fails(self):
        # An ended run leaves the runs list and queues what it held; an interrupted drain leaves that queue behind.
        state, run_id = two_runs()
        state = record.end(state, run_id, NINE, "failed", {"code": "lease-expired", "actions": []})
        generation = f"{NETWORK}:routine:{run_id}"
        self.assertEqual(state.discards, ((run_id, generation),))
        put(self.subject.routine_store, "team_1", state)

        def unavailable(_generation):
            raise action_journal.ActionJournalError("down")

        journal = self.subject.action_state
        self.subject.action_state = SimpleNamespace(purge=unavailable)
        with self.assertRaises(ApiProblemError) as caught:
            routine_lifecycle.delete_team_routines(self.subject, "team_1")
        self.assertEqual(caught.exception.code, "action-state-unavailable")
        self.assertEqual(self.subject.routine_store.load("team_1").discards, state.discards)

        self.subject.action_state = journal
        self.events.clear()
        routine_lifecycle.delete_team_routines(self.subject, "team_1")
        self.assertEqual(
            self.events,
            [("purge", generation)],
        )
        self.assertEqual(self.subject.routine_store.teams(), ())

    def test_a_space_reset_deletes_every_teams_routines_and_the_keyring(self):
        state, run_id = two_runs()
        for team in ("team_1", "team_2"):
            put(self.subject.routine_store, team, state)
        self.subject.routine_store.put_continuation("team_1", run_id, b"continuation")
        routine_lifecycle.delete_all_routines(self.subject)
        self.assertEqual(len([event for event in self.events if event[0] == "purge"]), 2)
        self.assertEqual(self.subject.routine_store.teams(), ())
        self.assertFalse(self.subject.routine_store.key_path.exists())

    def test_each_failure_is_a_retryable_unavailable_problem(self):
        state, _run_id = two_runs()
        put(self.subject.routine_store, "team_1", state)

        def fail(error: BaseException):
            def raise_error(*_args):
                raise error

            return raise_error

        cases = (("action_state", "purge", action_journal.ActionJournalError("down"), "action-state-unavailable"),)
        for owner, method, error, code in cases:
            with self.subTest(code=code):
                original = getattr(self.subject, owner)
                setattr(
                    self.subject,
                    owner,
                    SimpleNamespace(
                        **{method: fail(error)},
                        **{name: value for name, value in vars(original).items() if name != method},
                    ),
                )
                with self.assertRaises(ApiProblemError) as caught:
                    routine_lifecycle.delete_team_routines(self.subject, "team_1")
                self.assertEqual((caught.exception.status, caught.exception.code), (503, code))
                setattr(self.subject, owner, original)
        store = self.subject.routine_store
        for name, call in (
            ("load", lambda: routine_lifecycle.delete_team_routines(self.subject, "team_1")),
            ("delete", lambda: routine_lifecycle.delete_team_routines(self.subject, "team_1")),
            ("teams", lambda: routine_lifecycle.delete_all_routines(self.subject)),
            ("delete_all", lambda: routine_lifecycle.delete_all_routines(self.subject)),
        ):
            with self.subTest(name=name):
                fake = SimpleNamespace(
                    load=store.load,
                    delete=store.delete,
                    teams=lambda: (),
                    delete_all=store.delete_all,
                    lock=store.lock,
                    exclusive=store.exclusive,
                )
                setattr(fake, name, fail(routine_store.RoutineStoreError("broken")))
                self.subject.routine_store = fake
                with self.assertRaises(ApiProblemError) as caught:
                    call()
                self.assertEqual(caught.exception.code, "routine-state-unavailable")
        self.subject.routine_store = store

    def test_a_run_thread_names_only_a_valid_run(self):
        with self.assertRaises(ApiProblemError):
            routine_thread_id("local-space", "team_1", NETWORK, "not-a-run")


if __name__ == "__main__":
    unittest.main()
