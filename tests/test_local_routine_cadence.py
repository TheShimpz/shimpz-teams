"""Continuous Routines under load: caps, fairness, chat priority, backpressure, and concurrency (ADR-0092 section 9)."""

import dataclasses
import itertools
import json
import tempfile
import threading
import time
import unittest
from unittest import mock

import routine_fixture
from test_local_routine_http import CLAIM, RoutineHttpCase
from test_local_routine_service import Runtime

from local.routine import contracts as routine_contracts
from local.routine import run as routine_run
from protocol.http.v1 import routine as http_routine
from protocol.http.v1 import routine_notice as http_routine_notice
from routine import claim as routine_claim
from routine import record
from routine import runs as routine_runs
from routine import starts as routine_starts

CONTINUOUS = {"kind": "continuous", "gap": 5, "cap": http_routine.continuous_cap(5)}


def continuous(routine_id: str = "a" * 32, *, at: int = 1_800_000_000, gap: int = 5) -> record.Routine:
    schedule = {"kind": "continuous", "gap": gap, "cap": http_routine.continuous_cap(gap)}
    return routine_fixture.routine(routine_id, name="Zones", anchor=at, schedule=schedule)


def caught_up(state: record.TeamRoutines) -> record.TeamRoutines:
    """Admin delivered every notice and Team removed what ended runs held; either would hold every start back."""
    state = record.acknowledge(state, frozenset((item.notice_id, item.version) for item in state.notices))
    for run_id, generation in state.discards:
        state = record.discarded(state, run_id, generation)
    return state


class SimulatedDayTests(unittest.TestCase):
    """A simulated clock runs continuous Routines back to back for more than a day."""

    def test_back_to_back_runs_never_overlap_and_each_starts_its_gap_after_the_previous_ended(self) -> None:
        start = 1_800_000_000
        state = record.add_routine(record.TeamRoutines(), continuous(at=start - 5))
        now, starts, running = start, [], None
        key = "e" * 64
        while now < start + 86_400 and len(starts) < 100:
            if running is None:
                state, claim = routine_claim.claim(state, now, key)
                if claim is not None:
                    running, starts = claim.run.run_id, [*starts, now]
                    # A second claim while it runs finds nothing: one run at a time.
                    self.assertIsNone(routine_claim.claim(state, now, key)[1])
            elif now >= starts[-1] + 3:
                state = routine_runs.end(state, running, now, "stopped", {"actions": []})
                state = caught_up(state)
                running = None
                # The next run is due exactly its gap after this one ended.
                self.assertEqual(record.routine(state, "a" * 32).next_run_at, now + 5)
            due = routine_claim.next_due(state, now)
            now = now + 1 if due is None or running is not None else max(now + 1, due)
        # A hundred runs, each its gap after the previous ended: the cap is its whole day and never holds one back.
        gaps = {later - earlier for earlier, later in itertools.pairwise(starts)}
        self.assertEqual((len(starts), gaps), (100, {8}))

    def test_continuous_routines_take_turns_and_a_scheduled_one_due_earlier_goes_first(self) -> None:
        state = record.TeamRoutines()
        # Two one-step Routines every 10 seconds fill 17,280 of the Team's 20,000 daily steps.
        for routine_id in ("a" * 32, "b" * 32):
            state = record.add_routine(state, continuous(routine_id, at=1_800_000_000, gap=10))
        key, now, order = "e" * 64, 1_800_000_010, []
        for _turn in range(6):
            state, claim = routine_claim.claim(state, now, key)
            order.append(claim.run.routine_id)
            now += 3
            state = caught_up(routine_runs.end(state, claim.run.run_id, now, "stopped", {"actions": []}))
            now = routine_claim.next_due(state, now) or now
        # Neither continuous Routine ever starves the other.
        self.assertEqual(order, ["a" * 32, "b" * 32] * 3)

    def test_undelivered_notices_hold_every_start_and_every_wake_until_delivered(self) -> None:
        state = record.add_routine(record.TeamRoutines(), continuous(at=1_800_000_000))
        notices = tuple(
            record.Notice(f"{index:032x}", "a" * 32, f"{index:032x}", "stopped", 1, {"actions": []}, 1, "q")
            for index in range(record.MAX_UNDELIVERED_NOTICES)
        )
        full = dataclasses.replace(state, notices=notices)
        due = record.routine(full, "a" * 32).next_run_at
        self.assertEqual((routine_claim.claimable(full, due), routine_claim.next_due(full, due - 1)), (None, None))
        delivered = record.acknowledge(full, frozenset((item.notice_id, 1) for item in notices))
        self.assertEqual(routine_claim.claimable(delivered, due).routine_id, "a" * 32)
        self.assertEqual(routine_claim.next_due(delivered, due - 1), due)

    def test_the_team_window_holds_at_most_one_start_a_step_and_frees_one_at_a_time(self) -> None:
        starts = tuple(("a" * 32, 1000 + index, 1) for index in range(routine_starts.MAX_STARTS))
        self.assertEqual(routine_starts.free_at(starts, "b" * 32, None, 1000 + 86_399), 1000 + 86_400)
        self.assertEqual(routine_starts.free_at(starts, "b" * 32, None, 1000 + 86_400), 1000 + 86_400)
        self.assertEqual(len(routine_starts.started(starts, "b" * 32, 1000 + 86_400, 1)), routine_starts.MAX_STARTS)

    def test_the_team_window_reserves_every_step_each_start_may_run(self) -> None:
        """At most 20,000 business steps start in any rolling 24 hours, whatever started them (scale)."""
        steps = routine_starts.routine_plan.MAX_DAILY_STEPS
        starts = (("a" * 32, 1000, steps - 300), ("b" * 32, 2000, 200), ("a" * 32, 3000, 100))
        # Exactly full: the next start of even one step waits until enough of the window's starts leave it.
        self.assertEqual(routine_starts.free_at(starts, "c" * 32, None, 4000, 1), 1000 + 86_400)
        partial = starts[1:]
        self.assertEqual(routine_starts.free_at(partial, "c" * 32, None, 4000, 300), 4000)
        self.assertEqual(routine_starts.free_at(partial, "c" * 32, None, 4000, steps - 299), 2000 + 86_400)
        self.assertEqual(routine_starts.free_at(partial, "c" * 32, None, 4000, steps), 3000 + 86_400)

    def test_the_team_window_frees_its_oldest_start_whichever_routine_made_it(self) -> None:
        # Sorted by Routine id, "a" at 5 would look older than "b" at 0 and hold the budget 5 seconds too long.
        starts = (("b" * 32, 0, 1), ("a" * 32, 5, 1))
        with mock.patch.object(routine_starts.routine_plan, "MAX_DAILY_STEPS", 2):
            self.assertEqual(routine_starts.free_at(starts, "c" * 32, None, 10), 86_400)
            self.assertEqual(routine_starts.free_at(starts, "a" * 32, 2, 10), 86_400)
            self.assertEqual(routine_starts.free_at(starts, "a" * 32, 1, 10), 86_405)


class ServiceLoadTests(RoutineHttpCase):
    def continuous_routine(self, service, gap: int = 5) -> record.Routine:
        contracts = routine_contracts.current_contracts(service, "team_1", ("shimpz-cloudflare",))
        now = int(time.time())
        plan = self.plan(service)
        value = record.Routine(
            routine_id=record.new_id(),
            name="Zones",
            plan=plan,
            schedule={"kind": "continuous", "gap": gap, "cap": http_routine.continuous_cap(gap)},
            timezone="UTC",
            assistants=tuple(sorted(contracts.items())),
            anchor=now - 60,
            next_run_at=0,
            permitted=self.permitted(service, plan),
            confirmation=dict(routine_fixture.CONFIRMATION),
        )
        value = dataclasses.replace(value, next_run_at=record.next_after(value, value.anchor))
        service.routine_store.update("team_1", lambda state: (record.add_routine(state, value), None))
        return value

    def test_a_person_waiting_on_a_routine_gets_the_next_boundary_however_short_its_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.continuous_routine(service)
            # The chat message arrives while a run holds the slot.
            with (
                service._exclusive_chat_turn("team_1", value.routine_id),
                self.assertRaises(routine_run.ApiProblem) as refused,
                service._exclusive_chat_turn("team_1"),
            ):
                pass
            self.assertEqual(refused.exception.code, "routine-active")
            # The Routine is due again, but chat goes first: no run starts while the person waits.
            self.assertIsNone(service.claim_routine_run())
            with service._exclusive_chat_turn("team_1"):
                pass
            self.assertIsNotNone(service.claim_routine_run())

    def test_a_person_refused_early_in_a_long_segment_keeps_priority_until_after_it_ends(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.continuous_routine(service)
            clock = [1000.0]
            with mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]):
                with service._exclusive_chat_turn("team_1", value.routine_id):
                    with self.assertRaises(routine_run.ApiProblem), service._exclusive_chat_turn("team_1"):
                        pass
                    # The segment keeps running well past the bounded grace.
                    clock[0] += routine_run.CHAT_PRIORITY_SECONDS * 4
                # The slot is free: the person still goes first for the bounded grace, measured from now.
                clock[0] += routine_run.CHAT_PRIORITY_SECONDS - 1
                self.assertIsNone(service.claim_routine_run())
                clock[0] += 1
                self.assertIsNotNone(service.claim_routine_run())

    def test_a_waiting_person_holds_runs_back_only_for_a_bounded_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.continuous_routine(service)
            service._chat_demand["team_1"] = time.monotonic() - routine_run.CHAT_PRIORITY_SECONDS
            self.assertIsNotNone(service.claim_routine_run())

    def test_a_minute_rollup_reaches_admin_as_a_closed_routine_notice(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            value = self.continuous_routine(service)
            minute = int(time.time()) // 60 * 60
            usage = {"duration_ms": 3600, "models": []}
            rollup = record.Notice("c" * 32, value.routine_id, "", "healthy", minute, {"runs": 3}, 3, usage=usage)
            service.routine_store.update("team_1", lambda state: (record._notice(state, rollup), None))
            batch = service.routine_notices()
        self.assertEqual(http_routine_notice.canonical_notice_batch(batch), batch)
        (notice,) = [item for item in batch["notices"] if item["outcome"] == "healthy"]
        self.assertEqual((notice["run_id"], notice["version"], notice["created_at"][-3:]), (None, 3, "00Z"))
        # The rollup names the Routine as it was and carries its runs' summed usage.
        self.assertEqual((notice["name"], notice["usage"], notice["protection_lost"]), ("Zones", usage, False))

    def test_a_team_leases_one_run_at_a_time_however_many_of_its_routines_are_due(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.service(directory, Runtime())
            self.continuous_routine(service, gap=10)
            self.continuous_routine(service, gap=10)
            first = service.claim_routine_run()
            self.assertIsNotNone(first)
            # Its other due Routine waits for the leased run's end, which wakes Admin; it is not hinted meanwhile.
            self.assertEqual((service.claim_routine_run(), service.next_routine_due()), (None, None))

    def test_concurrent_http_claims_lease_one_run_per_routine_and_never_overlap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _controller, service = self.serve(directory, Runtime())
            self.continuous_routine(service)
            barrier = threading.Barrier(8)
            answers: list[dict[str, object]] = []

            def claim() -> None:
                barrier.wait(5)
                _status, _type, raw = self.request("POST", "/v1/routines/claim", CLAIM)
                answers.append(json.loads(raw))

            workers = [threading.Thread(target=claim) for _index in range(8)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(10)
            state = self.state(service)
        runs = [answer["run"] for answer in answers if answer["run"] is not None]
        self.assertEqual((len(answers), len(runs), len(state.runs)), (8, 1, 1))
        self.assertEqual(runs[0]["mode"], "continuous")
        # The others get no wake hint for a Routine that is running: its own end sets its next run.
        self.assertTrue(all(answer["next_due_at"] is None for answer in answers if answer["run"] is None))
