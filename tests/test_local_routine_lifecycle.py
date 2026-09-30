"""Destroying a Team or resetting the Space removes every Routine run's Brain thread, journal generation, and state."""

from __future__ import annotations

import dataclasses
import datetime
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from action import journal as action_journal
from inference import client as brain_runtime_client
from local.errors import ApiProblemError
from local.routine import lifecycle as routine_lifecycle
from local.routine import proposal as routine_proposal
from local.routine import store as routine_store
from local.validation import routine_thread_id
from routine import record

KEY = "e" * 64
PROPOSED = {
    "op": "propose",
    "quote": "Every day at 9, check the DNS",
    "schedule": {"kind": "daily", "time": "09:00"},
    "timezone": None,
    "routine_id": None,
}
NETWORK = "a" * 64
NINE = int(datetime.datetime(2026, 10, 1, 9, tzinfo=datetime.UTC).timestamp())


def put(store: routine_store.RoutineStore, team_id: str, state: record.TeamRoutines) -> None:
    """Replace a Team's Routine state through the store's only write path."""
    store.update(team_id, lambda _before: (state, None))


def routine(routine_id: str) -> record.Routine:
    value = record.Routine(
        routine_id=routine_id,
        quote="Every day at 9, check the DNS.",
        schedule={"kind": "daily", "time": "09:00"},
        timezone="UTC",
        assistants=(("dns", "sha256:" + "c" * 64),),
        anchor=NINE - 86_400,
        next_run_at=0,
    )
    return dataclasses.replace(value, next_run_at=record.next_after(value, value.anchor))


def two_runs() -> tuple[record.TeamRoutines, str]:
    """One run that bound its generation and one that has not started a segment yet."""
    state = record.add_routine(record.add_routine(record.TeamRoutines(), routine("a" * 32)), routine("b" * 32))
    state, bound = record.claim(state, NINE, KEY)
    state = record.bind_generation(state, bound.run.run_id, record.lease_of(bound.lease_token, KEY), NINE, NETWORK)
    state, _fresh = record.claim(state, NINE, KEY)
    return state, bound.run.run_id


class RoutineLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.events: list[object] = []
        self.subject = SimpleNamespace(
            space_id="local-space",
            routine_store=routine_store.RoutineStore(root / "state", root / "key" / "aes256.key"),
            brain_runtime=SimpleNamespace(delete_thread=lambda thread_id: self.events.append(("thread", thread_id))),
            action_state=SimpleNamespace(purge=lambda generation: self.events.append(("purge", generation))),
            routine_proposals=routine_proposal.ProposalBook(),
        )

    def test_a_teams_routine_threads_generations_and_state_are_deleted(self):
        state, run_id = two_runs()
        put(self.subject.routine_store, "team_1", state)
        self.subject.routine_store.put_continuation("team_1", run_id, b"continuation")
        proposed = self.subject.routine_proposals.create("team_1", PROPOSED, {"dns": "sha256:" + "c" * 64})
        routine_lifecycle.delete_team_routines(self.subject, "team_1")
        with self.assertRaises(routine_proposal.ProposalError):
            self.subject.routine_proposals.peek("team_1", proposed.proposal_id)
        self.assertEqual(
            self.events,
            [
                ("thread", routine_thread_id("local-space", "team_1", NETWORK, run_id)),
                ("purge", f"{NETWORK}:routine:{run_id}"),
            ],
        )
        self.assertEqual(
            (self.subject.routine_store.teams(), self.subject.routine_store.continuations("team_1")), ((), ())
        )
        # An absent Team is already clean.
        routine_lifecycle.delete_team_routines(self.subject, "team_1")

    def test_a_space_reset_deletes_every_teams_routines_and_the_keyring(self):
        # A proposal of a Team without any Routine state yet is dropped too.
        orphan = self.subject.routine_proposals.create("team_9", PROPOSED, {"dns": "sha256:" + "c" * 64})
        state, run_id = two_runs()
        for team in ("team_1", "team_2"):
            put(self.subject.routine_store, team, state)
        self.subject.routine_store.put_continuation("team_1", run_id, b"continuation")
        routine_lifecycle.delete_all_routines(self.subject)
        with self.assertRaises(routine_proposal.ProposalError):
            self.subject.routine_proposals.peek("team_9", orphan.proposal_id)
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

        cases = (
            ("brain_runtime", "delete_thread", brain_runtime_client.BrainRuntimeError("down"), "brain-runtime-failed"),
            ("action_state", "purge", action_journal.ActionJournalError("down"), "action-state-unavailable"),
        )
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
