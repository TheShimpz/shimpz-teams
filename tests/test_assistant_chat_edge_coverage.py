"""Close the fail-closed edges of the shared Assistant and chat contracts."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from assistant import genesis as assistant_genesis
from assistant import spec as assistant_spec
from chat import contract as chat_contract
from chat import orchestrator as chat_orchestrator
from chat import progress as chat_progress
from chat import turn as chat_turn
from inference import client as brain_runtime_client
from integrations import challenges as integration_challenges
from integrations import flow as integration_flow
from tests.test_assistant_genesis import Container, manifest
from tests.test_chat_orchestrator import FakeRuntime, accept_input, context, strategy, suspended, suspension


def _request(interrupt_id: str) -> brain_runtime_client.PowerRequest:
    return suspended(interrupt_id=interrupt_id).powers[0]


def _prepared(identity: tuple[object, ...] = ("identity",)) -> chat_turn.PreparedSegment:
    batch = SimpleNamespace(prepare=lambda _batch: None, invoke=lambda _request: {}, delivered=lambda _batch: None)
    return chat_turn.PreparedSegment("Team", identity, context(), [], batch)


def _segment_strategy(**overrides) -> chat_turn.SegmentStrategy:
    values = {
        "runtime": FakeRuntime([]),
        "prepare": _prepared,
        "validate_power": accept_input,
        "pause_for_private_inputs": lambda _requests, _requirements: False,
        "cancelled": lambda: False,
        "validate_context": lambda: None,
        "raise_problem": lambda reason, _exc: (_ for _ in ()).throw(RuntimeError(reason)),
    }
    values.update(overrides)
    return chat_turn.SegmentStrategy(**values)


class AssistantContractEdgeCoverageTests(unittest.TestCase):
    def test_genesis_cache_rejects_each_invalid_capacity_shape(self) -> None:
        for value in ("1", True, 0):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "positive"):
                assistant_genesis.GenesisCache(value)

    def test_genesis_cache_rejects_invalid_container_identities(self) -> None:
        cache = assistant_genesis.GenesisCache()
        for identity in (None, "", "a" * 257, "bad/id"):
            with (
                self.subTest(identity=identity),
                self.assertRaisesRegex(
                    assistant_genesis.GenesisError,
                    "identity",
                ),
            ):
                cache.get(SimpleNamespace(id=identity))

    def test_genesis_cache_discard_is_typed_and_forgets_a_cached_generation(self) -> None:
        container = Container("generation", manifest("Safe guidance."))
        cache = assistant_genesis.GenesisCache()

        self.assertEqual(cache.get(container), "Safe guidance.")
        cache.discard(object())
        self.assertEqual(container.reads, 1)
        cache.discard(container.id)
        self.assertEqual(cache.get(container), "Safe guidance.")
        self.assertEqual(container.reads, 2)

    def test_unknown_power_payload_direction_fails_closed(self) -> None:
        power = assistant_spec.PowerSpec("Summary", {}, {})

        with self.assertRaisesRegex(ValueError, "direction"):
            assistant_spec.validate_power_payload(power, "sideways", {})

    def test_malformed_decision_and_power_input_are_redacted_contract_errors(self) -> None:
        for raw in (None, "{"):
            with self.subTest(raw=raw), self.assertRaisesRegex(chat_contract.ChatContractError, "decision"):
                chat_contract.parse_decision(raw, max_message_chars=100, max_input_bytes=100)
        with self.assertRaisesRegex(chat_contract.ChatContractError, "Power input"):
            chat_contract.parse_decision(
                '{"kind":"power","message":"","power":"lookup","input":"{"}',
                max_message_chars=100,
                max_input_bytes=100,
            )


class ChatOrchestratorEdgeCoverageTests(unittest.TestCase):
    def test_invalid_suspension_batches_fail_before_invocation(self) -> None:
        duplicate = _request("duplicate")
        cases = (
            (suspension(), accept_input, "without a Power"),
            (suspension(duplicate, duplicate), accept_input, "repeated a Power interrupt id"),
            (suspension(_request("one")), lambda *_args: [], "invalid input contract"),
        )
        for turn, validator, message in cases:
            with (
                self.subTest(message=message),
                self.assertRaisesRegex(
                    chat_orchestrator.ChatOrchestrationError,
                    message,
                ),
            ):
                chat_orchestrator.run(
                    FakeRuntime([turn]),
                    context(),
                    "Run",
                    strategy(validator, lambda _request: self.fail("Power must not run")),
                )

    def test_cancellation_between_start_and_drive_stops_the_turn(self) -> None:
        decisions = iter((False, True))

        with self.assertRaisesRegex(chat_orchestrator.ChatStoppedError, "stopped"):
            chat_orchestrator.run_until_pause(
                FakeRuntime([suspended()]),
                context(),
                "Run",
                strategy(accept_input, lambda _request: {}, cancelled=lambda: next(decisions)),
            )

    def test_cancellation_between_batch_members_stops_remaining_side_effects(self) -> None:
        decisions = iter((False, False, False, True))
        invoked: list[str] = []

        with self.assertRaisesRegex(chat_orchestrator.ChatStoppedError, "stopped"):
            chat_orchestrator.run_until_pause(
                FakeRuntime([suspension(_request("one"), _request("two"))]),
                context(),
                "Run",
                strategy(
                    accept_input,
                    lambda request: invoked.append(request.interrupt_id),
                    cancelled=lambda: next(decisions),
                ),
            )
        self.assertEqual(invoked, ["one"])

    def test_continuation_rejects_an_already_seen_interrupt_before_invocation(self) -> None:
        continuation = chat_orchestrator.ChatContinuation(suspended(), ("interrupt-1",), (), 0)

        with self.assertRaisesRegex(chat_orchestrator.ChatOrchestrationError, "across rounds"):
            chat_orchestrator.continue_after_pause(
                FakeRuntime([]),
                context(),
                continuation,
                strategy(accept_input, lambda _request: self.fail("Power must not run")),
            )

    def test_invalid_continuation_round_and_noncontinuable_run_fail_closed(self) -> None:
        beyond_limit = chat_orchestrator.ChatContinuation(
            suspended(),
            (),
            (),
            chat_orchestrator.MAX_POWER_ROUNDS + 1,
        )
        with self.assertRaisesRegex(chat_orchestrator.ChatOrchestrationError, "did not complete"):
            chat_orchestrator.continue_after_pause(
                FakeRuntime([]),
                context(),
                beyond_limit,
                strategy(accept_input, None),
            )

        with self.assertRaisesRegex(chat_orchestrator.ChatOrchestrationError, "paused"):
            chat_orchestrator.run(
                FakeRuntime([suspended()]),
                context(),
                "Run",
                strategy(accept_input, lambda _request: {}, pause_before_batch=lambda _batch: True),
            )


class ChatProgressEdgeCoverageTests(unittest.TestCase):
    def test_default_sink_and_capped_emit_are_noops(self) -> None:
        chat_progress._ignore({})
        chat_progress.Reporter()._emit("model", "started")
        with chat_progress.Reporter().span("model"):
            pass

        reporter = chat_progress.Reporter([].append, sequence=chat_progress.MAX_SEQUENCE)
        reporter._emit("model", "started")
        self.assertEqual(reporter.sequence, chat_progress.MAX_SEQUENCE)

    def test_emit_rejects_every_invalid_event_shape(self) -> None:
        reporter = chat_progress.Reporter([].append)
        calls = (
            ("unknown", "started", {}),
            ("model", "unknown", {}),
            ("power", "started", {"index": 1}),
            ("model", "started", {"index": 1, "total": 1}),
            ("model", "started", {"elapsed_ms": 0}),
            ("model", "finished", {"elapsed_ms": None}),
        )
        for phase, state, kwargs in calls:
            with self.subTest(phase=phase, state=state, kwargs=kwargs), self.assertRaises(ValueError):
                reporter._emit(phase, state, **kwargs)


class ChatTurnEdgeCoverageTests(unittest.TestCase):
    def _resume_strategy(self, store, **overrides) -> chat_turn.IntegrationResumeStrategy:
        values = {
            "store": store,
            "team_id": "team_1",
            "challenge_id": "challenge",
            "pending_valid": lambda _pending: True,
            "pending_identity": lambda _pending: ("identity",),
            "inspect": lambda _pending: chat_turn.IntegrationResumeContext(("identity",), (), ()),
            "integration_store": object(),
            "challenge_response": lambda _challenge: "response",
            "expired_error": lambda: RuntimeError("expired"),
            "context_error": lambda: RuntimeError("context"),
            "contract_error": lambda: RuntimeError("contract"),
        }
        values.update(overrides)
        return chat_turn.IntegrationResumeStrategy(**values)

    def test_integration_resume_rejects_missing_invalid_and_drifted_challenges(self) -> None:
        missing_store = mock.Mock()
        missing_store.get.side_effect = integration_challenges.IntegrationChallengeNotFoundError("missing")
        with self.assertRaisesRegex(RuntimeError, "expired"):
            chat_turn.admit_integration_resume(self._resume_strategy(missing_store))

        challenge = SimpleNamespace(id="challenge", payload=object())
        store = mock.Mock(get=mock.Mock(return_value=challenge))
        with self.assertRaisesRegex(RuntimeError, "context"):
            chat_turn.admit_integration_resume(self._resume_strategy(store, pending_valid=lambda _pending: False))

        with self.assertRaisesRegex(RuntimeError, "context"):
            chat_turn.admit_integration_resume(
                self._resume_strategy(
                    store,
                    inspect=lambda _pending: chat_turn.IntegrationResumeContext(("changed",), (), ()),
                )
            )
        store.cancel_team.assert_called_once_with("team_1")

    def test_integration_resume_handles_contract_missing_and_one_use_outcomes(self) -> None:
        challenge = SimpleNamespace(id="challenge", payload=object())
        store = mock.Mock(get=mock.Mock(return_value=challenge), claim=mock.Mock(return_value=challenge))
        strategy = self._resume_strategy(store)

        with (
            mock.patch.object(
                chat_turn.integration_flow,
                "requirements_for_batch",
                side_effect=integration_flow.IntegrationFlowError("bad"),
            ),
            self.assertRaisesRegex(RuntimeError, "contract"),
        ):
            chat_turn.admit_integration_resume(strategy)

        with mock.patch.object(chat_turn.integration_flow, "requirements_for_batch", return_value=("missing",)):
            self.assertEqual(
                chat_turn.admit_integration_resume(strategy),
                chat_turn.IntegrationResumeAdmission(None, "response"),
            )

        store.claim.side_effect = integration_challenges.IntegrationChallengeNotFoundError("gone")
        with (
            mock.patch.object(chat_turn.integration_flow, "requirements_for_batch", return_value=()),
            self.assertRaisesRegex(RuntimeError, "expired"),
        ):
            chat_turn.admit_integration_resume(strategy)

        store.claim.side_effect = None
        store.claim.return_value = SimpleNamespace()
        with (
            mock.patch.object(chat_turn.integration_flow, "requirements_for_batch", return_value=()),
            self.assertRaisesRegex(RuntimeError, "expired"),
        ):
            chat_turn.admit_integration_resume(strategy)

        store.claim.return_value = challenge
        with mock.patch.object(chat_turn.integration_flow, "requirements_for_batch", return_value=()):
            self.assertEqual(
                chat_turn.admit_integration_resume(strategy),
                chat_turn.IntegrationResumeAdmission(challenge.payload, None),
            )

    def test_segment_adapter_rejects_invalid_inputs_context_and_drive_failures(self) -> None:
        for message, continuation in ((None, None), ("message", object())):
            with self.subTest(message=message), self.assertRaisesRegex(RuntimeError, "invalid-continuation"):
                chat_turn.run_segment(
                    _segment_strategy(),
                    message=message,
                    continuation=continuation,
                    expected_identity=None,
                )

        with self.assertRaisesRegex(RuntimeError, "context-changed"):
            chat_turn.run_segment(
                _segment_strategy(),
                message="message",
                continuation=None,
                expected_identity=("different",),
            )

        with (
            mock.patch.object(chat_turn, "drive", side_effect=chat_orchestrator.ChatStoppedError("stopped")),
            self.assertRaisesRegex(AssertionError, "adapter returned"),
        ):
            chat_turn.run_segment(
                _segment_strategy(raise_problem=lambda _reason, _exc: None),
                message="message",
                continuation=None,
                expected_identity=("identity",),
            )

    def test_segment_adapter_rejects_a_suspension_without_exactly_one_gate(self) -> None:
        outcome = chat_orchestrator.ChatSuspension(
            chat_orchestrator.ChatContinuation(suspended(), (), (), 0),
            suspended().powers,
        )
        with (
            mock.patch.object(chat_turn, "drive", return_value=outcome),
            self.assertRaisesRegex(RuntimeError, "invalid-suspension"),
        ):
            chat_turn.run_segment(
                _segment_strategy(),
                message="message",
                continuation=None,
                expected_identity=("identity",),
            )

    def test_dispatch_rejects_invalid_gate_shapes_and_unreachable_state(self) -> None:
        outcome = chat_orchestrator.ChatSuspension(
            chat_orchestrator.ChatContinuation(suspended(), (), (), 0),
            suspended().powers,
        )
        for requirements, pause in ((((),), ()), (((),), (lambda *_args: None,))):
            with self.subTest(requirements=requirements), self.assertRaisesRegex(ValueError, "invalid chat suspension"):
                chat_turn.dispatch(outcome, requirements, lambda _outcome: object(), pause, lambda _outcome: None)

        with self.assertRaisesRegex(AssertionError, "unreachable"):
            chat_turn._raise_unreachable_suspension()
        with (
            mock.patch.object(chat_turn, "suspension_gate_count", return_value=1),
            self.assertRaisesRegex(AssertionError, "unreachable"),
        ):
            chat_turn.dispatch(
                outcome,
                ((),),
                lambda _outcome: object(),
                (lambda *_args: None,),
                lambda _outcome: None,
            )


if __name__ == "__main__":
    unittest.main()
