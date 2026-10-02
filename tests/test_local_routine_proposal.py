"""A chat turn's Routine change becomes a one-use, Team-bound proposal a Supervisor must confirm (ADR-0086)."""

from __future__ import annotations

import tempfile
import threading
import unittest
from types import SimpleNamespace

from local_controller_harness import LocalContractCase

from inference import client as brain_runtime_client
from local import app as local_app
from local.routine import proposal as routine_proposal
from local.routine import turn as routine_turn

CHANGE = {
    "op": "propose",
    "quote": "Every Monday at 9, check the DNS",
    "schedule": {"kind": "weekly", "weekday": 0, "time": "09:00"},
    "timezone": None,
    "routine_id": None,
}

DIGEST = "sha256:" + "c" * 64


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0

    def __call__(self) -> float:
        return self.now


class ProposalBookTests(unittest.TestCase):
    def test_a_proposal_is_one_use_team_bound_and_expires(self) -> None:
        clock = Clock()
        book = routine_proposal.ProposalBook(clock)
        proposal = book.create("team_1", CHANGE, {"dns": DIGEST})
        self.assertRegex(proposal.proposal_id, r"\A[0-9a-f]{32}\Z")
        self.assertEqual(
            proposal.view(clock.now),
            {"proposal_id": proposal.proposal_id, **CHANGE, "assistant_ids": ["dns"], "expires_in": 900},
        )
        self.assertEqual(book.peek("team_1", proposal.proposal_id), proposal)
        for team, proposal_id in (("team_2", proposal.proposal_id), ("team_1", "0" * 32), ("team_1", ["x"])):
            with self.subTest(team=team, proposal_id=proposal_id), self.assertRaises(routine_proposal.ProposalError):
                book.peek(team, proposal_id)
            with self.subTest(take=team), self.assertRaises(routine_proposal.ProposalError):
                book.take(team, proposal_id)
        self.assertEqual(book.take("team_1", proposal.proposal_id), proposal)
        with self.assertRaises(routine_proposal.ProposalError):
            book.take("team_1", proposal.proposal_id)
        expiring = book.create("team_1", CHANGE, {"dns": DIGEST})
        clock.now += routine_proposal.PROPOSAL_TTL_SECONDS
        with self.assertRaises(routine_proposal.ProposalError):
            book.peek("team_1", expiring.proposal_id)
        self.assertEqual(expiring.view(clock.now + 5)["expires_in"], 0)

    def test_only_a_closed_change_is_proposed_and_the_oldest_gives_way(self) -> None:
        clock = Clock()
        book = routine_proposal.ProposalBook(clock)
        with self.assertRaises(routine_proposal.ProposalError):
            book.create("team_1", {**CHANGE, "quote": "a\nb"}, {"dns": DIGEST})
        first = book.create("team_1", CHANGE, {"dns": DIGEST})
        for _index in range(routine_proposal.MAX_PROPOSALS):
            clock.now += 1
            book.create("team_2", CHANGE, {"dns": DIGEST})
        with self.assertRaises(routine_proposal.ProposalError):
            book.peek("team_1", first.proposal_id)
        book.drop_team("team_2")
        self.assertEqual(book._live(), {})
        other = book.create("team_1", CHANGE, {"dns": DIGEST})
        book.drop("team_2", other.proposal_id)
        self.assertEqual(book.peek("team_1", other.proposal_id), other)
        book.drop("team_1", other.proposal_id)
        book.drop("team_1", other.proposal_id)
        with book.fenced(), self.assertRaisesRegex(routine_proposal.ProposalError, "being reset"):
            book.create("team_1", CHANGE, {"dns": DIGEST})
        self.assertEqual(book.create("team_1", CHANGE, {"dns": DIGEST}).team_id, "team_1")


class ChatProposalTests(LocalContractCase):
    def test_a_committed_turn_with_a_routine_change_returns_its_proposal(self) -> None:
        class Runtime:
            @staticmethod
            def start(_context, _message, *, conversation=()):
                return brain_runtime_client.RuntimeTurn(
                    status="completed", reply="Confirme para agendar.", actions=(), routine=CHANGE
                )

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, Runtime())
            controller.routine_proposals = routine_proposal.ProposalBook()
            controller.chat_turn_service.routine_proposals = controller.routine_proposals
            response = controller.chat_turn_service.chat(
                "team_1",
                {
                    "message": CHANGE["quote"],
                    "files": [],
                    "assistant_ids": ["shimpz-cloudflare"],
                    "conversation": [],
                    "request": {"issued_at": 1_700_000_000, "nonce": "0" * 32},
                    "timezone": None,
                    "locale": None,
                },
                "openai",
                "sk-test-0123456789",
            )
        proposal = response["routine_proposal"]
        self.assertEqual({key: proposal[key] for key in CHANGE}, CHANGE)
        self.assertEqual(proposal["assistant_ids"], ["shimpz-cloudflare"])
        self.assertEqual(controller.routine_proposals.peek("team_1", proposal["proposal_id"]).team_id, "team_1")
        self.assertIsNone(routine_turn.routine_proposal(SimpleNamespace(), SimpleNamespace(), None))

    def test_a_proposal_binds_the_turns_exact_contracts_under_the_team_lock(self) -> None:
        def docker_down(_team_id, _network):
            raise local_app.ApiProblem(503, "Docker is unavailable", code="docker-unavailable")

        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, SimpleNamespace())
            service = controller.chat_turn_service
            service.routine_proposals = routine_proposal.ProposalBook()
            ids = ("shimpz-cloudflare",)
            identity = service._chat_identity(*service._chat_setup("team_1", [], "openai", ids))
            seen = {"shimpz-cloudflare": DIGEST}
            response = SimpleNamespace(
                team_id="team_1",
                segment=SimpleNamespace(identity=identity, contracts=tuple(seen.items())),
                file_ids=(),
                provider="openai",
                assistant_ids=ids,
            )
            crossed: list[bool] = []
            create = service.routine_proposals.create

            def create_while_locked(team_id, change, contracts):
                # A destroy or reset needs this Team lock, so it cannot run between validation and insertion.
                attempt = threading.Thread(
                    target=lambda: crossed.append(service._lock(team_id).acquire(blocking=False))
                )
                attempt.start()
                attempt.join(5)
                return create(team_id, change, contracts)

            service.routine_proposals.create = create_while_locked
            view = routine_turn.routine_proposal(service, response, CHANGE)
            self.assertEqual(crossed, [False])
            self.assertEqual(
                service.routine_proposals.peek("team_1", view["proposal_id"]).contracts, tuple(seen.items())
            )
            routine_turn.withdraw_routine_proposal(service, "team_1", view)
            routine_turn.withdraw_routine_proposal(service, "team_1", None)
            with self.assertRaises(routine_proposal.ProposalError):
                service.routine_proposals.peek("team_1", view["proposal_id"])
            service.routine_proposals.create = create
            with service.routine_proposals.fenced(), self.assertRaises(local_app.ApiProblem) as closed:
                routine_turn.routine_proposal(service, response, CHANGE)
            self.assertEqual(closed.exception.code, "routine-proposal-unavailable")
            changed = SimpleNamespace(
                **{**vars(response), "segment": SimpleNamespace(identity=("other",), contracts=())}
            )
            with self.assertRaises(local_app.ApiProblem) as moved:
                routine_turn.routine_proposal(service, changed, CHANGE)
            self.assertEqual(moved.exception.code, "team-context-changed")
            self.assertRegex(routine_turn.current_contracts(service, "team_1", ids)["shimpz-cloudflare"], r"\Asha256:")
            # An Assistant the Team does not run is proven absent; a Team that cannot be read proves nothing.
            self.assertEqual(routine_turn.current_contracts(service, "team_1", ("shimpz-absent",)), {})
            service._active_chat_assistants = docker_down
            with self.assertRaises(routine_turn.ContractsUnavailableError):
                routine_turn.current_contracts(service, "team_1", ids)

    def test_a_message_while_a_routine_runs_is_told_why_it_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._chat_controller(directory, SimpleNamespace())
            service = controller.chat_turn_service
            entered, release = threading.Event(), threading.Event()

            def hold(routine_id: str | None) -> None:
                with service._exclusive_chat_turn("team_1", routine_id):
                    entered.set()
                    release.wait(5)

            for routine_id, code in (("a" * 32, "routine-active"), (None, "chat-active")):
                entered.clear()
                release.clear()
                holder = threading.Thread(target=hold, args=(routine_id,))
                holder.start()
                entered.wait(5)
                with (
                    self.subTest(code=code),
                    self.assertRaises(local_app.ApiProblem) as caught,
                    service._exclusive_chat_turn("team_1"),
                ):
                    pass
                release.set()
                holder.join(5)
                self.assertEqual(caught.exception.code, code)
            self.assertEqual(service._routine_holders, {})


if __name__ == "__main__":
    unittest.main()
