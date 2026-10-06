"""Local Routine state survives restarts exactly, fails closed when altered, and is removed without residue."""

from __future__ import annotations

import dataclasses
import datetime
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import routine_fixture

from local.chat import continuation as local_chat_continuation
from local.routine import store as routine_store
from routine import hold as routine_hold
from routine import record

UTC = datetime.UTC
KEY = "e" * 64
NETWORK = "a" * 64
NINE = int(datetime.datetime(2026, 10, 1, 9, tzinfo=UTC).timestamp())


def put(store: routine_store.RoutineStore, team_id: str, state: record.TeamRoutines) -> None:
    """Replace a Team's Routine state through the store's only write path."""
    store.update(team_id, lambda _before: (state, None))


def routine(routine_id: str = "a" * 32) -> record.Routine:
    value = routine_fixture.confirmed(
        record.Routine(
            routine_id=routine_id,
            name="Resumo de DNS",
            plan=routine_fixture.plan_document(timezone="America/Sao_Paulo"),
            schedule={"kind": "daily", "time": "09:00"},
            timezone="America/Sao_Paulo",
            assistants=(),
            anchor=NINE - 86_400,
            next_run_at=0,
        )
    )
    return dataclasses.replace(value, next_run_at=record.next_after(value, value.anchor))


USAGE = {
    "duration_ms": 1200,
    "models": [
        {"provider": "openai", "model": f"gpt-{index:02d}", "input_tokens": 10**9, "output_tokens": 10**9}
        for index in range(16)
    ],
}


def decision_routine(routine_id: str = "d" * 32) -> record.Routine:
    """A decision Routine at every scope field: no replay step, a prompt, a model, an allowance, and a baseline."""
    plan = routine_fixture.plan_document(timezone="UTC", output={"mode": "decide", "step": None, "when": "changes"})
    plan["steps"] = []
    value = dataclasses.replace(
        routine(routine_id),
        plan=plan,
        timezone="UTC",
        schedule={"kind": "hourly", "every": 1},
        permitted=(),
        assistants=(),
        prompt="sha256:" + "1" * 64,
        model={"provider": "openai", "model": "gpt-6-luna", "effort": "low"},
        allowance=16,
        baseline={"id": "2" * 32, "digest": "3" * 64},
        permissions_revision=2,
        rollup_usage={"duration_ms": 5, "models": []},
    )
    return dataclasses.replace(value, next_run_at=record.next_after(value, value.anchor))


def busy_state() -> record.TeamRoutines:
    """Every kind of run, notice, and queued removal, as a real Team accumulates them."""
    state = record.add_routine(record.add_routine(record.TeamRoutines(), routine()), routine("b" * 32))
    state = record.add_routine(state, routine("e" * 32))
    now = routine().next_run_at + 60
    state, first = record.claim(state, now, KEY)
    lease = record.lease_of(first.lease_token, KEY)
    state = record.bind_generation(state, first.run.run_id, lease, now, NETWORK)
    position = {"phase": "replay", "step": 1}
    state = record.freeze(state, first.run.run_id, lease, now, ("human", "dns", "check", position))
    state, second = record.claim(state, now, KEY)
    lease = record.lease_of(second.lease_token, KEY)
    state = record.bind_generation(state, second.run.run_id, lease, now, NETWORK)
    state = routine_hold.fence(state, second.run.run_id, lease, now)
    state, third = record.claim(state, now, KEY)
    lease = record.lease_of(third.lease_token, KEY)
    state = record.bind_generation(state, third.run.run_id, lease, now, NETWORK)
    return record.end(state, third.run.run_id, now, "stopped", {"actions": []})


class StoreCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.store = routine_store.RoutineStore(root / "state", root / "key" / "aes256.key")

    def state_file(self, team_id: str = "team_1") -> Path:
        return self.store._team_dir(team_id) / "state.json"


class RoundTripTests(StoreCase):
    def test_a_team_state_round_trips_exactly_and_an_absent_one_is_empty(self):
        self.assertEqual(self.store.load("team_1"), record.TeamRoutines())
        state = record.add_routine(busy_state(), decision_routine())
        run = dataclasses.replace(state.runs[0], usage=USAGE, protection_lost=True)
        state = dataclasses.replace(state, runs=(run, *state.runs[1:]))
        put(self.store, "team_1", state)
        self.assertEqual(self.store.load("team_1"), state)
        self.assertEqual(self.state_file().stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.teams(), ("team_1",))

    def test_update_persists_only_a_changed_state_and_returns_its_result(self):
        self.assertEqual(self.store.update("team_1", lambda state: (state, "same")), "same")
        self.assertFalse(self.state_file().exists())
        added = self.store.update("team_1", lambda state: (record.add_routine(state, routine()), "added"))
        self.assertEqual((added, len(self.store.load("team_1").routines)), ("added", 1))
        self.assertIs(self.store.lock("team_1"), self.store.lock("team_1"))

    def test_an_invalid_team_or_an_oversized_state_is_refused(self):
        for team in ("Team", "", 1):
            with self.subTest(team=team), self.assertRaisesRegex(routine_store.RoutineStoreError, "Team is invalid"):
                self.store.load(team)
        worst = dataclasses.replace(
            busy_state(),
            notices=tuple(
                record.Notice(
                    f"{index:032x}",
                    "a" * 32,
                    f"{index:032x}",
                    "done",
                    NINE,
                    routine_fixture.large_completion(),
                    1,
                    "\U0001f600" * 80,
                    USAGE,
                    True,
                )
                for index in range(record.MAX_UNDELIVERED_NOTICES + record.MAX_ROUTINE_NOTICES)
            ),
        )
        put(self.store, "team_1", worst)
        self.assertEqual(self.store.load("team_1"), worst)
        with (
            mock.patch.object(routine_store, "MAX_STATE_BYTES", 1024),
            self.assertRaisesRegex(routine_store.RoutineStoreError, "byte limit"),
        ):
            put(self.store, "team_2", worst)

    def test_the_largest_state_a_team_can_admit_fits_its_derived_bound(self):
        """Every definition at the Team's budget, every notice, start, and incident at its own (scale)."""

        def large(routine_id: str, inputs: int) -> record.Routine:
            plan = routine_fixture.plan_document(timezone="America/Sao_Paulo")
            plan["steps"] = [
                {
                    **plan["steps"][0],
                    "id": f"s{index}",
                    "input": {f"m{member}": {"kind": "literal", "value": "\u00e9" * 8} for member in range(inputs)},
                }
                for index in range(routine_store.routine_plan.MAX_STEPS)
            ]
            plan["output"] = {"mode": "none", "step": None, "when": None}
            return routine_fixture.confirmed(
                dataclasses.replace(routine(routine_id), plan=plan, name="\U0001f600" * 80)
            )

        state = record.TeamRoutines()
        # Four plans near their own bound fill the Team's definition budget; the rest are small.
        for index, inputs in enumerate((17, 17, 17, 16)):
            state = record.add_routine(state, large(f"{index:032x}", inputs))
        for index in range(4, record.MAX_ROUTINES):
            state = record.add_routine(state, routine(f"{index:032x}"))
        definitions = sum(routine_store.routine_definition.definition_bytes(item) for item in state.routines)
        self.assertGreater(definitions, routine_store.routine_plan.TEAM_DEFINITION_BYTES * 0.9)
        name = "\U0001f600" * 80
        notices = tuple(
            record.Notice(
                f"{index:032x}",
                "a" * 32,
                f"{index:032x}",
                "done",
                NINE,
                routine_fixture.large_completion(),
                1,
                name,
                USAGE,
                True,
            )
            for index in range(record.MAX_UNDELIVERED_NOTICES + record.MAX_ROUTINE_NOTICES)
        )
        self.assertGreater(
            max(len(json.dumps(item.detail).encode()) for item in notices), routine_store.MAX_NOTICE_BYTES // 2
        )
        incidents = tuple(
            record.Incident(f"{index:032x}", "a" * 32, f"{NETWORK}:routine:{index:032x}", NINE, name=name, usage=USAGE)
            for index in range(record.MAX_INCIDENTS)
        )
        worst = dataclasses.replace(
            state,
            notices=notices,
            starts=tuple(("a" * 32, NINE + index, 256) for index in range(record.routine_starts.MAX_STARTS)),
            incidents=incidents,
            discards=tuple(
                (f"{index:032x}", f"{NETWORK}:routine:{index:032x}") for index in range(record.MAX_DISCARDS)
            ),
        )
        self.assertLessEqual(len(routine_store._encode(worst, "team_1")), routine_store.MAX_STATE_BYTES)

    def test_the_keyring_must_live_outside_the_state_root(self):
        root = Path(self.directory.name)
        with self.assertRaisesRegex(routine_store.RoutineStoreError, "separate"):
            routine_store.RoutineStore(root / "state", root / "state" / "aes256.key")


class TamperTests(StoreCase):
    def write(self, value: object, team_id: str = "team_1") -> None:
        """Replace the state file directly, as a corrupting writer would, keeping its private mode."""
        path = self.store._team_dir(team_id) / "state.json"
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        path.chmod(0o600)

    def baseline(self) -> dict[str, object]:
        put(self.store, "team_1", busy_state())
        return json.loads(self.state_file().read_text())

    def assert_refused(self, value: object) -> None:
        self.write(value)
        with self.assertRaises(routine_store.RoutineStoreError):
            self.store.load("team_1")

    def test_every_altered_field_fails_closed(self):
        base = self.baseline()
        frozen = next(index for index, item in enumerate(base["runs"]) if item["status"] == "frozen")
        held = next(index for index, item in enumerate(base["runs"]) if item["status"] == "held")
        mutations = {
            "schema": lambda value: value.update(schema=1),
            "schema type": lambda value: value.update(schema=float(routine_store.SCHEMA)),
            "a retired rehearsal proof": lambda value: value["routines"][0].update(rehearsed=None),
            "run steps type": lambda value: value["runs"][held].update(steps=False),
            "team": lambda value: value.update(team_id="team_2"),
            "extra field": lambda value: value.update(extra=1),
            "routine shape": lambda value: value["routines"][0].pop("confirmation"),
            "unconfirmed": lambda value: value["routines"][0].update(confirmation=None),
            "confirmation principal": lambda value: value["routines"][0]["confirmation"].update(principal="person"),
            "no permitted Action for the step": lambda value: value["routines"][0].update(permitted=[]),
            "permitted pin drift": lambda value: value["routines"][0]["permitted"][0].update(pin="sha256:" + "0" * 64),
            "permitted read-only type": lambda value: value["routines"][0]["permitted"][0].update(read_only=1),
            "a model without a decision": lambda value: value["routines"][0].update(
                model={"provider": "openai", "model": "gpt-6-luna", "effort": "low"}
            ),
            "an allowance without a decision": lambda value: value["routines"][0].update(allowance=1),
            "a prompt without a decision": lambda value: value["routines"][0].update(prompt="sha256:" + "1" * 64),
            "a baseline without a decision": lambda value: value["routines"][0].update(
                baseline={"id": "2" * 32, "digest": "3" * 64}
            ),
            "a retired rehearsal": lambda value: value["routines"][0].update(rehearsal=False),
            "paused type": lambda value: value["routines"][0].update(paused=1),
            "permissions revision": lambda value: value["routines"][0].update(permissions_revision=-1),
            "rollup usage": lambda value: value["routines"][0].update(rollup_usage={"duration_ms": 1}),
            "assistants beyond the permitted": lambda value: value["routines"][0].update(
                assistants=[*value["routines"][0]["assistants"], ["web", "sha256:" + "a" * 64]]
            ),
            "output digest": lambda value: value["routines"][0].update(output_digest="x"),
            "schedule": lambda value: value["routines"][0].update(schedule={"kind": "daily", "time": "25:00"}),
            "timezone": lambda value: value["routines"][0].update(timezone="../etc"),
            "assistants": lambda value: value["routines"][0].update(assistants=[["dns", "md5"]]),
            "reconfirm type": lambda value: value["routines"][0].update(needs_reconfirm=1),
            "negative instant": lambda value: value["routines"][0].update(next_run_at=-1),
            "duplicate routine": lambda value: value["routines"].append(dict(value["routines"][0])),
            "run status": lambda value: value["runs"][0].update(status="running"),
            "lease key": lambda value: value["runs"][0].update(lease_key="short"),
            "frozen with lease": lambda value: value["runs"][frozen].update(lease_sha256="d" * 64, lease_key=KEY),
            "held without a generation": lambda value: value["runs"][held].update(generation=""),
            "held with a request": lambda value: value["runs"][held].update(request_kind="human"),
            "held with a lease": lambda value: value["runs"][held].update(lease_key=KEY),
            "frozen without an action": lambda value: value["runs"][frozen].update(action=""),
            "frozen without an assistant": lambda value: value["runs"][frozen].update(assistant_id=""),
            "arbitrary generation": lambda value: value["runs"][held].update(generation="other:routine:x"),
            "generation of another run": lambda value: value["runs"][frozen].update(
                generation=NETWORK + ":routine:" + "0" * 32
            ),
            "unknown status": lambda value: value["runs"][0].update(status="paused"),
            "orphan run": lambda value: value["runs"][0].update(routine_id="f" * 32),
            "active time": lambda value: value["runs"][0].update(active_seconds_left=record.ACTIVE_SECONDS + 1),
            "answered requests": lambda value: value["runs"][0].update(requests_used=17),
            "answered requests type": lambda value: value["runs"][0].update(requests_used=True),
            "notice detail": lambda value: value["notices"][0].update(detail={"actions": [["dns", "x"]], "result": 1}),
            "notice version": lambda value: value["notices"][0].update(version=0),
            "notice name": lambda value: value["notices"][0].update(name=""),
            "notice usage": lambda value: value["notices"][0].update(usage=None),
            "notice protection type": lambda value: value["notices"][0].update(protection_lost=1),
            "run usage": lambda value: value["runs"][0].update(usage={"duration_ms": 1}),
            "a retired run rehearsal": lambda value: value["runs"][0].update(rehearsal=False),
            "run protection type": lambda value: value["runs"][0].update(protection_lost=0),
            "frozen position past its plan": lambda value: value["runs"][frozen].update(
                position={"phase": "replay", "step": 2}
            ),
            "frozen without a position": lambda value: value["runs"][frozen].update(position=None),
            "held with a position": lambda value: value["runs"][held].update(
                position={"phase": "replay", "step": 1}, steps=1
            ),
            "run notice version": lambda value: value["runs"][0].update(notice_version=-1),
            "retired run field": lambda value: value["runs"][held].update(batch=["", ""]),
            "discard shape": lambda value: value["discards"][0].append("x"),
            "discard run": lambda value: value["discards"][0].__setitem__(0, "not-a-run"),
            "discard of another generation": lambda value: value["discards"][0].__setitem__(
                1, NETWORK + ":routine:" + "0" * 32
            ),
            "duplicate discard": lambda value: value["discards"].append(list(value["discards"][0])),
            "too many discards": lambda value: value.update(
                discards=[[f"{index:032x}", ""] for index in range(record.MAX_DISCARDS + 1)]
            ),
            "rollup runs": lambda value: value["routines"][0].update(
                rollup_runs=record.http_routine.MAX_ROLLUP_RUNS + 1
            ),
            "rollup minute": lambda value: value["routines"][0].update(rollup_minute=-1),
            "starts shape": lambda value: value.update(starts=[["a" * 32]]),
            "start without steps": lambda value: value.update(starts=[["a" * 32, 5]]),
            "start routine": lambda value: value.update(starts=[["not-a-routine", 5, 1]]),
            "start instant": lambda value: value.update(starts=[["a" * 32, -1, 1]]),
            "start of no step": lambda value: value.update(starts=[["a" * 32, 5, 0]]),
            "start of too many steps": lambda value: value.update(starts=[["a" * 32, 5, 257]]),
            "start steps type": lambda value: value.update(starts=[["a" * 32, 5, True]]),
            "starts out of order": lambda value: value.update(starts=[["a" * 32, 9, 1], ["a" * 32, 5, 1]]),
            "too many starts": lambda value: value.update(
                starts=[["a" * 32, index, 1] for index in range(record.routine_starts.MAX_STARTS + 1)]
            ),
            # A definition the Team's budgets never admit is never loaded either (ADR-0092, 2026-10-05, scale).
            "definition over its budget": lambda value: value["routines"][0]["plan"]["steps"][0].update(
                input={f"m{index}": {"kind": "literal", "value": "x" * 100} for index in range(4000)}
            ),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                value = json.loads(json.dumps(base))
                mutate(value)
                self.assert_refused(value)
        self.assert_refused([])
        self.state_file().write_bytes(b"{not json")
        with self.assertRaisesRegex(routine_store.RoutineStoreError, "not valid JSON"):
            self.store.load("team_1")

    def test_an_altered_incident_fails_closed(self):
        state = busy_state()
        held = next(item for item in state.runs if item.status == "held")
        step = ("dns", "check", {"phase": "replay", "step": 1}, 1)
        put(self.store, "team_1", routine_hold.settle_hold(state, held.run_id, NINE, 1, step))
        base = json.loads(self.state_file().read_text())
        self.assertEqual(base["incidents"][0]["assistant_id"], "dns")
        mutations = {
            "name": {"name": ""},
            "assistant": {"assistant_id": "Bad"},
            "half a call": {"action": ""},
            "action type": {"action": 1},
            "time beyond the run's": {"active_seconds_left": record.ACTIVE_SECONDS + 1},
            "time type": {"active_seconds_left": True},
            "answered requests past the turn's": {"requests_used": 17},
            "a call with no position": {"position": None},
            "a position past its plan": {"position": {"phase": "replay", "step": 2}, "steps": 1},
            "a plan of too many steps": {"steps": 257},
            "position type": {"position": {"phase": "replay", "step": True}},
            "a decision call past the allowance": {"position": {"phase": "decision", "call": 65}},
            "no call but a position": {"assistant_id": "", "action": "", "steps": 1},
            "no call with a boolean count": {"assistant_id": "", "action": "", "position": None, "steps": False},
            "no call with a float count": {"assistant_id": "", "action": "", "position": None, "steps": 0.0},
            "usage": {"usage": {"duration_ms": 1}},
            "a retired rehearsal": {"rehearsal": False},
            "protection type": {"protection_lost": "no"},
        }
        for name, change in mutations.items():
            with self.subTest(name=name):
                value = json.loads(json.dumps(base))
                value["incidents"][0].update(change)
                self.assert_refused(value)
        value = json.loads(json.dumps(base))
        value["incidents"][0].update(assistant_id="", action="", position=None, steps=0)
        self.write(value)
        self.assertEqual(self.store.load("team_1").incidents[0].action, "")
        value["incidents"][0].update(
            assistant_id="dns", action="check", position={"phase": "decision", "call": 3}, steps=0
        )
        self.write(value)
        self.assertEqual(self.store.load("team_1").incidents[0].position, {"phase": "decision", "call": 3})

    def test_a_state_file_that_is_not_private_fails_closed(self):
        put(self.store, "team_1", busy_state())
        self.state_file().chmod(0o644)
        with self.assertRaisesRegex(routine_store.RoutineStoreError, "ownership"):
            self.store.load("team_1")

    def test_a_listing_refuses_state_filed_under_another_team(self):
        put(self.store, "team_1", busy_state())
        self.store._team_dir("team_2").mkdir(mode=0o700)
        self.state_file().replace(self.store._team_dir("team_2") / "state.json")
        with self.assertRaises(routine_store.RoutineStoreError):
            self.store.teams()
        (self.store._team_dir("team_2") / "state.json").write_bytes(b"[")
        with self.assertRaises(routine_store.RoutineStoreError):
            self.store.teams()


class ContinuationTests(StoreCase):
    def test_a_continuation_is_encrypted_and_bound_to_its_team_and_run(self):
        payload = b'{"turn": "secret-free continuation"}'
        self.store.put_continuation("team_1", "a" * 32, payload)
        path = self.store._team_dir("team_1") / f"{'a' * 32}.continuation"
        self.assertNotIn(b"secret-free", path.read_bytes())
        self.assertEqual(self.store.continuation("team_1", "a" * 32), payload)
        self.assertEqual(self.store.continuations("team_1"), ("a" * 32,))
        self.store._team_dir("team_2").mkdir(mode=0o700)
        (self.store._team_dir("team_2") / f"{'a' * 32}.continuation").write_bytes(path.read_bytes())
        (self.store._team_dir("team_2") / f"{'a' * 32}.continuation").chmod(0o600)
        with self.assertRaisesRegex(routine_store.RoutineStoreError, "authentication failed"):
            self.store.continuation("team_2", "a" * 32)
        path.replace(self.store._team_dir("team_1") / f"{'b' * 32}.continuation")
        with self.assertRaisesRegex(routine_store.RoutineStoreError, "authentication failed"):
            self.store.continuation("team_1", "b" * 32)

    def test_invalid_missing_and_tampered_continuations_fail_closed(self):
        for payload in (b"", "text", b"x" * (local_chat_continuation.MAX_ROUTINE_BYTES + 1)):
            with self.subTest(size=len(payload)), self.assertRaisesRegex(routine_store.RoutineStoreError, "invalid"):
                self.store.put_continuation("team_1", "a" * 32, payload)
        with self.assertRaisesRegex(routine_store.RoutineStoreError, "Routine run is invalid"):
            self.store.put_continuation("team_1", "A" * 32, b"x")
        with self.assertRaisesRegex(routine_store.RoutineStoreError, "unavailable"):
            self.store.continuation("team_1", "a" * 32)
        self.store.put_continuation("team_1", "a" * 32, b"payload")
        path = self.store._team_dir("team_1") / f"{'a' * 32}.continuation"
        # An envelope whose nonce or ciphertext is not the sealed shape is malformed, never an authentication failure.
        short = {"algorithm": "AES-256-GCM", "nonce": "AAAA", "ciphertext": "A" * 24}
        for content in (
            b"[",
            json.dumps({"algorithm": "none", "nonce": "", "ciphertext": ""}).encode(),
            json.dumps(short).encode(),
        ):
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaisesRegex(routine_store.RoutineStoreError, "malformed"):
                    self.store.continuation("team_1", "a" * 32)
        self.store.delete_continuation("team_1", "a" * 32)
        self.store.delete_continuation("team_1", "a" * 32)
        self.assertEqual(self.store.continuations("team_1"), ())
        self.assertEqual(self.store.continuations("team_9"), ())


class DeletionTests(StoreCase):
    def test_a_team_and_the_whole_space_are_removed_without_residue(self):
        put(self.store, "team_1", busy_state())
        self.store.put_continuation("team_1", "a" * 32, b"payload")
        put(self.store, "team_2", busy_state())
        self.store.delete("team_1")
        self.store.delete("team_1")
        self.assertEqual(self.store.teams(), ("team_2",))
        self.store.delete_all()
        self.store.delete_all()
        self.assertEqual((self.store.teams(), list(self.store.root.iterdir())), ((), []))
        self.assertFalse(self.store.key_path.exists())

    def test_a_deleted_team_releases_its_lock_and_a_held_lock_is_shared(self):
        put(self.store, "team_1", busy_state())
        held = self.store.lock("team_2")
        with mock.patch.object(routine_store.threading, "RLock", wraps=threading.RLock) as created:
            self.assertIs(self.store.lock("team_2"), held)
            self.assertIs(self.store.lock("team_2"), held)
        # A lookup that finds a live lock allocates nothing.
        created.assert_not_called()
        self.store.delete("team_1")
        self.assertNotIn("team_1", self.store._locks)
        del held
        self.assertEqual(len(self.store._locks), 0)


class FilesystemFailureTests(StoreCase):
    def test_listing_and_removal_failures_fail_closed(self):
        put(self.store, "team_1", busy_state())
        self.store._team_dir("team_3").mkdir(mode=0o700)
        self.assertEqual(self.store.teams(), ("team_1",))
        denied = PermissionError("denied")
        for target, calls, message in (
            ("scandir", (self.store.teams, self.store.delete_all), "could not be listed"),
            ("open", (lambda: self.store.delete("team_1"),), "could not be listed"),
            ("unlink", (lambda: self.store.delete("team_1"), self.store.delete_all), "could not be removed"),
        ):
            for call in calls:
                with (
                    self.subTest(target=target, call=call),
                    mock.patch.object(routine_store.os, target, side_effect=denied),
                    self.assertRaisesRegex(routine_store.RoutineStoreError, message),
                ):
                    call()
        with (
            mock.patch.object(Path, "iterdir", side_effect=denied),
            self.assertRaisesRegex(routine_store.RoutineStoreError, "could not be listed"),
        ):
            self.store.continuations("team_1")
        with (
            mock.patch.object(Path, "unlink", side_effect=denied),
            self.assertRaisesRegex(routine_store.RoutineStoreError, "could not be removed"),
        ):
            self.store.delete_continuation("team_1", "a" * 32)
        empty = routine_store.RoutineStore(Path(self.directory.name) / "absent", Path(self.directory.name) / "k" / "k")
        self.assertEqual(empty.teams(), ())
        empty.delete_all()
        empty.delete("team_1")
        with (
            mock.patch.object(Path, "unlink", side_effect=denied),
            self.assertRaisesRegex(routine_store.RoutineStoreError, "keyring could not be removed"),
        ):
            empty.delete_all()

    def test_deletion_never_follows_a_link_or_removes_a_directory_it_does_not_own(self):
        outside = Path(self.directory.name) / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("keep")
        put(self.store, "team_1", busy_state())
        self.store.delete("team_2")
        (self.store.root / ("f" * 64)).symlink_to(outside, target_is_directory=True)
        for call in (self.store.teams, self.store.delete_all):
            with self.subTest(call=call), self.assertRaisesRegex(routine_store.RoutineStoreError, "ownership"):
                call()
        self.assertTrue((outside / "keep.txt").exists())
        (self.store.root / ("f" * 64)).unlink()
        (self.store._team_dir("team_1") / "nested").mkdir()
        with self.assertRaisesRegex(routine_store.RoutineStoreError, "ownership"):
            self.store.delete("team_1")


class ExclusionTests(StoreCase):
    def test_a_reset_refuses_writes_during_it_and_from_before_it(self):
        put(self.store, "team_1", busy_state())
        started = self.store._current_epoch()
        with self.store.exclusive():
            for write in (
                lambda: self.store.update("team_1", lambda state: (record.TeamRoutines(), None)),
                lambda: self.store.put_continuation("team_1", "a" * 32, b"payload"),
            ):
                with self.subTest(write=write), self.assertRaisesRegex(routine_store.RoutineStoreError, "reset"):
                    write()
        with self.assertRaisesRegex(routine_store.RoutineStoreError, "reset"):
            self.store._writable(started)
        self.assertEqual(self.store.update("team_1", lambda state: (state, "after")), "after")

    def test_a_reset_waits_for_a_write_in_flight(self):
        entered, release = threading.Event(), threading.Event()
        order: list[str] = []

        def slow(state: record.TeamRoutines) -> tuple[record.TeamRoutines, None]:
            entered.set()
            release.wait(5)
            order.append("write")
            return record.add_routine(state, routine()), None

        writer = threading.Thread(target=lambda: self.store.update("team_1", slow))
        writer.start()
        entered.wait(5)
        resetting = threading.Thread(target=lambda: self._reset(order))
        resetting.start()
        release.set()
        writer.join(5)
        resetting.join(5)
        self.assertEqual(order, ["write", "reset"])
        self.assertEqual(self.store.teams(), ())

    def _reset(self, order: list[str]) -> None:
        with self.store.exclusive():
            order.append("reset")
            self.store.delete_all()

    def test_concurrent_first_continuations_share_one_keyring(self):
        teams = [f"team_{index}" for index in range(8)]
        threads = [
            threading.Thread(target=self.store.put_continuation, args=(team, "a" * 32, team.encode())) for team in teams
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertEqual([self.store.continuation(team, "a" * 32) for team in teams], [team.encode() for team in teams])


if __name__ == "__main__":
    unittest.main()


class ConcurrentReadTests(StoreCase):
    def test_a_listing_read_that_races_a_replace_reads_again_and_a_lasting_failure_still_fails(self) -> None:
        put(self.store, "team_1", busy_state())
        real = routine_store._PRIVATE.read_private_file
        failures = [routine_store.RoutineStoreError("Routine state failed its ownership contract")] * 2

        def racing(path, maximum, label):
            if failures:
                raise failures.pop()
            return real(path, maximum, label)

        with mock.patch.object(type(routine_store._PRIVATE), "read_private_file", side_effect=racing):
            self.assertEqual(self.store.teams(), ("team_1",))
        broken = routine_store.RoutineStoreError("Routine state failed its ownership contract")
        with (
            mock.patch.object(type(routine_store._PRIVATE), "read_private_file", side_effect=broken),
            self.assertRaisesRegex(routine_store.RoutineStoreError, "ownership"),
        ):
            self.store.teams()


class StartWindowTests(StoreCase):
    def test_alternating_routines_persist_their_starts_in_time_order_and_rollups_round_trip(self) -> None:
        first = 1_790_000_000
        starts: record.routine_starts.Starts = ()
        for routine_id, offset in (("a" * 32, 0), ("b" * 32, 3), ("a" * 32, 8), ("b" * 32, 11)):
            starts = record.routine_starts.started(starts, routine_id, first + offset, 1)
        self.assertEqual([at - first for _routine_id, at, _steps in starts], [0, 3, 8, 11])
        state = dataclasses.replace(busy_state(), starts=starts)
        rolled = dataclasses.replace(state.routines[0], rollup_minute=first - first % 60, rollup_runs=12)
        state = dataclasses.replace(state, routines=(rolled, *state.routines[1:]))
        put(self.store, "team_1", state)
        self.assertEqual(self.store.load("team_1").starts, starts)
