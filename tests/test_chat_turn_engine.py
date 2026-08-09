"""Characterize the shared hosted/local chat-segment decision engine."""

from __future__ import annotations

import contextlib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TEAM))
sys.path.insert(0, str(TESTS))

import hosted_assistant_fixture as hosted_harness

from assistant import spec as assistant_registry
from chat import orchestrator as chat_orchestrator
from chat import turn as chat_turn_engine
from inference import client as brain_runtime_client
from inference import config as inference_config
from local import app as local_app
from local.chat import segment as local_chat_segment
from local.chat.segment import SegmentRequest
from local.chat.types import ActiveAssistant
from local.install.runtime import AssistantSpec
from power import human as power_human

hosted_app = hosted_harness.app


def _context() -> brain_runtime_client.RuntimeContext:
    return brain_runtime_client.RuntimeContext(
        thread_id="thread-1",
        team_name="Team",
        assistants=(
            brain_runtime_client.RuntimeAssistant(
                id="assistant",
                genesis="Use the declared Power.",
                powers=(
                    brain_runtime_client.RuntimePower(
                        id="lookup",
                        summary="Look up one value.",
                        input_schema={"type": "object"},
                    ),
                ),
            ),
        ),
        provider="openai",
        model="gpt-test",
        api_key="test-key",
    )


class _Runtime:
    @staticmethod
    def start(_context, _message):
        return brain_runtime_client.RuntimeTurn(
            status="power-required",
            reply="",
            powers=(
                brain_runtime_client.PowerRequest(
                    interrupt_id="interrupt-1",
                    assistant_id="assistant",
                    power="lookup",
                    input={"query": "Ada"},
                ),
            ),
        )


class _Batch:
    @staticmethod
    def prepare(_requests) -> None:
        raise AssertionError("a suspended batch must not be prepared")

    @staticmethod
    def invoke(_request):
        raise AssertionError("a suspended Power must not be invoked")

    @staticmethod
    def delivered(_requests) -> None:
        raise AssertionError("a suspended batch must not be delivered")


def _local_controller(local_active, config, events: list[str], fail):
    controller = object.__new__(local_app.LocalController)
    controller.space_id = "local-space"
    controller.brain_runtime = SimpleNamespace()
    controller.power_state = SimpleNamespace(purge_replayable=lambda _generation: False)
    controller._lock = lambda _team_id: contextlib.nullcontext()
    controller.storage = SimpleNamespace(
        metadata=lambda _team_id, _files, _connection=None: [],
        metadata_connection=lambda _team_id, _files: contextlib.nullcontext(None),
    )
    controller.inference_store = SimpleNamespace(load=lambda _team_id: config)
    controller._wire_collaborators()
    controller.assistant_lifecycle._network = lambda _team_id: SimpleNamespace(id="a" * 64, name="team-network")
    controller.assistant_lifecycle._validate_network = lambda _network, _team_id, **_kwargs: "Team"
    controller.chat_turn_service._active_chat_assistants = lambda _team_id, _network_name: (local_active,)
    controller.assistant_lifecycle._active_assistant_genesis = lambda _active: "Use the declared Power."

    def local_private_inputs(_team_id, _bindings, _requests, requirements) -> bool:
        requirements.integrations = ("integration-required",)
        return True

    controller.chat_turn_service._require_chat_private_inputs = local_private_inputs
    controller.chat_turn_service._require_power_rpc_envelope = lambda *_args: events.append("preflight")
    controller.chat_turn_service._power_integration_generations = lambda *_args: events.append("integrations") or ()
    controller.chat_turn_service._chat_cancelled = lambda _token: False
    controller.chat_turn_service._validate_chat_context = lambda *_args: None
    controller.chat_turn_service._raise_chat_problem = lambda reason, _exc: fail(reason)
    return controller


def _context_contract(prepared) -> tuple[object, ...]:
    context = prepared.context
    assistants = tuple(
        (
            assistant.id,
            assistant.genesis,
            tuple((power.id, power.summary, power.input_schema) for power in assistant.powers),
        )
        for assistant in context.assistants
    )
    return context.team_name, assistants, context.provider, context.model, context.api_key, prepared.files


class SharedChatTurnEngineTest(unittest.TestCase):
    def test_human_suspension_populates_only_the_human_gate(self) -> None:
        descriptor = {
            "kind": "approval",
            "ordinal": 0,
            "title": "Continue",
            "description": "Continue the reviewed Power operation.",
        }
        descriptor["fingerprint"] = power_human._fingerprint(descriptor)
        admitted = power_human.validate_request(descriptor, ("approval",))

        class Batch:
            @staticmethod
            def prepare(_requests) -> None:
                return None

            @staticmethod
            def invoke(_request):
                raise power_human.HumanRequestSuspensionError(admitted)

            @staticmethod
            def delivered(_requests) -> None:
                raise AssertionError("a human-suspended batch must not be delivered")

        strategy = chat_turn_engine.SegmentStrategy(
            runtime=_Runtime(),
            prepare=lambda: chat_turn_engine.PreparedSegment("Team", ("identity",), _context(), [], Batch()),
            validate_power=lambda _assistant, _power, payload: payload,
            pause_for_private_inputs=lambda _requests, _requirements: False,
            cancelled=lambda: False,
            validate_context=lambda: None,
            raise_problem=lambda reason, _exc: self.fail(reason),
            human_requirement=lambda power, request: (power.interrupt_id, request.fingerprint),
        )

        result = chat_turn_engine.run_segment(
            strategy,
            message="Run the Power",
            continuation=None,
            expected_identity=("identity",),
        )

        self.assertIsInstance(result[2], chat_orchestrator.ChatHumanSuspension)
        self.assertEqual(result[3].integrations, ())
        self.assertEqual(result[3].human, (("interrupt-1", admitted.fingerprint),))

    def _strategy(self, *, decisions: list[str]) -> chat_turn_engine.SegmentStrategy:
        def private_inputs(_requests, requirements) -> bool:
            requirements.integrations = ("integration-required",)
            decisions.append("integrations")
            return True

        def raise_problem(reason: str, _exc: BaseException | None) -> None:
            raise AssertionError(reason)

        return chat_turn_engine.SegmentStrategy(
            runtime=_Runtime(),
            prepare=lambda: chat_turn_engine.PreparedSegment(
                "Team",
                ("identity",),
                _context(),
                [],
                _Batch(),
            ),
            validate_power=lambda _assistant, _power, payload: payload,
            pause_for_private_inputs=private_inputs,
            cancelled=lambda: False,
            validate_context=lambda: None,
            raise_problem=raise_problem,
        )

    def test_hosted_and_local_strategies_make_the_same_real_suspension_decision(self) -> None:
        decisions: dict[str, list[str]] = {"hosted": [], "local": []}

        hosted = chat_turn_engine.run_segment(
            self._strategy(decisions=decisions["hosted"]),
            message="look this up",
            continuation=None,
            expected_identity=("identity",),
        )
        local = chat_turn_engine.run_segment(
            self._strategy(decisions=decisions["local"]),
            message="look this up",
            continuation=None,
            expected_identity=("identity",),
        )

        self.assertIsInstance(hosted[2], chat_orchestrator.ChatSuspension)
        self.assertIsInstance(local[2], chat_orchestrator.ChatSuspension)
        self.assertEqual(hosted[2], local[2])
        self.assertEqual(hosted[3].integrations, local[3].integrations)
        self.assertEqual(decisions, {"hosted": ["integrations"], "local": ["integrations"]})

    def test_matching_suspension_commits_without_rollback(self) -> None:
        decisions: list[str] = []

        chat_turn_engine.commit_suspension(
            "continuation",
            "continuation",
            lambda: decisions.append("commit") is None,
            lambda: decisions.append("cancel"),
            lambda: RuntimeError("stopped"),
            lambda: decisions.append("cleanup"),
        )

        self.assertEqual(decisions, ["commit"])

    def test_terminal_drive_error_abandons_only_the_current_uncertain_batch(self) -> None:
        decisions: list[str] = []

        class Batch:
            @staticmethod
            def prepare(_requests) -> None:
                decisions.append("prepare")

            @staticmethod
            def invoke(_request):
                decisions.append("invoke")
                raise RuntimeError("Assistant RPC failed")

            @staticmethod
            def delivered(_requests) -> None:
                raise AssertionError("a failed batch must not be delivered")

            @staticmethod
            def abandon_uncertain() -> None:
                decisions.append("abandon")

        strategy = chat_turn_engine.SegmentStrategy(
            runtime=_Runtime(),
            prepare=lambda: chat_turn_engine.PreparedSegment("Team", ("identity",), _context(), [], Batch()),
            validate_power=lambda _assistant, _power, payload: payload,
            pause_for_private_inputs=lambda _requests, _requirements: False,
            cancelled=lambda: False,
            validate_context=lambda: None,
            raise_problem=lambda reason, _exc: self.fail(reason),
        )

        with self.assertRaisesRegex(RuntimeError, "Assistant RPC failed"):
            chat_turn_engine.run_segment(
                strategy,
                message="Run the Power",
                continuation=None,
                expected_identity=("identity",),
            )

        self.assertEqual(decisions, ["prepare", "invoke", "abandon"])

    def test_stale_or_failed_suspension_rolls_back_before_error(self) -> None:
        def assert_rollback(continuation: str, commit_result: bool, expected: list[str]) -> None:
            decisions: list[str] = []

            with self.assertRaisesRegex(RuntimeError, "stopped"):
                chat_turn_engine.commit_suspension(
                    continuation,
                    "continuation",
                    lambda: decisions.append("commit") is None and commit_result,
                    lambda: decisions.append("cancel"),
                    lambda: RuntimeError("stopped"),
                    lambda: decisions.append("cleanup"),
                )

            self.assertEqual(decisions, expected)

        assert_rollback("stale", True, ["cancel", "cleanup"])
        assert_rollback("continuation", False, ["commit", "cancel", "cleanup"])

    def test_hosted_context_validation_reuses_the_prepared_inventory(self) -> None:
        anchor = SimpleNamespace(id="a" * 64, labels={"team.name": "Team"})
        config = inference_config.InferenceConfig("openai", "gpt-test")
        turn_token = "turn-token"

        def run_with_validation(strategy, **_kwargs):
            prepared = strategy.prepare()
            strategy.validate_context()
            return (
                prepared.team_name,
                prepared.identity,
                chat_orchestrator.ChatOutcome("done", ()),
                chat_turn_engine.SegmentRequirements(),
            )

        with (
            mock.patch.multiple(
                hosted_harness.hosted_assistants,
                _active_team_assistants=lambda _team_id: (),
                _model_credential=lambda _owner, _provider, *_args: ("test-key", 7),
                _require_model_credential_current=lambda *_args: None,
            ),
            mock.patch.object(
                hosted_harness.runtime_state,
                "_inference_store",
                SimpleNamespace(load=lambda _team_id: config),
            ),
            mock.patch.object(
                hosted_harness.hosted_chat_segment,
                "_current_team_anchor",
                side_effect=lambda *_args: anchor,
            ),
            mock.patch.object(
                hosted_harness.runtime_state,
                "_storage",
                side_effect=lambda: SimpleNamespace(metadata=lambda _team_id, _files, *_args: []),
            ),
            mock.patch.object(
                hosted_harness.hosted_chat_segment,
                "_hosted_chat_setup",
                wraps=hosted_harness.hosted_chat_segment._hosted_chat_setup,
            ) as setup,
            mock.patch.object(
                hosted_harness.hosted_chat_segment.chat_turn_engine,
                "run_segment",
                side_effect=run_with_validation,
            ),
        ):
            result = hosted_harness.hosted_chat_segment._run_hosted_chat_segment(
                hosted_harness.hosted_chat_segment.HostedChatSegmentRequest(
                    team_id="team_1",
                    file_ids=[],
                    assistant_ids=(),
                    token=turn_token,
                    container=anchor,
                    owner="owner",
                    message="Hello",
                )
            )

        self.assertEqual(result.outcome.reply, "done")
        self.assertEqual(setup.call_args.args[:5], ("team_1", [], (), anchor, "owner"))
        self.assertIsNone(setup.call_args.args[5])
        self.assertIsNotNone(setup.call_args.args[6])

    def test_hosted_and_local_controllers_build_equivalent_real_segment_strategies(self) -> None:
        assistant_id = "shimpz-cloudflare"
        declared_contract = hosted_harness.HOSTED_SPEC.contract
        declared_power = declared_contract.powers["list-zones"]
        hosted_power = assistant_registry.PowerSpec(
            declared_power.summary,
            declared_power.input_schema,
            declared_power.output_schema,
        )
        hosted_contract = assistant_registry.AssistantContract(
            {"list-zones": hosted_power},
            {},
        )
        assistant_container = SimpleNamespace(id="b" * 64)
        hosted_active = hosted_harness.hosted_assistants._ActiveAssistant(
            assistant_id,
            hosted_contract,
            assistant_container,
        )

        local_power = assistant_registry.PowerSpec(
            declared_power.summary,
            dict(declared_power.input_schema),
            dict(declared_power.output_schema),
            (),
        )
        local_spec = AssistantSpec(
            assistant_id=assistant_id,
            name="Assistant",
            summary="Test Assistant",
            image="example.invalid/assistant@sha256:" + ("c" * 64),
            powers={"list-zones": local_power},
            allowed_hosts=(),
            required_image_labels=(
                ("org.shimpz.assistant.id", assistant_id),
                ("org.shimpz.source.digest", "sha256:" + ("d" * 64)),
            ),
        )
        local_active = ActiveAssistant(local_spec, assistant_container.id)
        request = SimpleNamespace(
            interrupt_id="interrupt-1",
            assistant_id=assistant_id,
            power="list-zones",
            input={"page": 1, "per_page": 25},
        )
        config = inference_config.InferenceConfig("openai", "gpt-test")
        captures: dict[str, tuple[object, object, chat_turn_engine.SegmentRequirements, bool]] = {}

        def capture(label: str):
            def run(strategy, **_kwargs):
                prepared = strategy.prepare()
                requirements = chat_turn_engine.SegmentRequirements()
                paused = strategy.pause_for_private_inputs((request,), requirements)
                captures[label] = (strategy, prepared, requirements, paused)
                return prepared.team_name, prepared.identity, SimpleNamespace(), requirements

            return run

        hosted_events: list[str] = []
        hosted_anchor = SimpleNamespace(id="a" * 64, labels={"team.name": "Team"})
        hosted_token = "turn-token"
        with (
            mock.patch.multiple(
                hosted_harness.hosted_assistants,
                _active_team_assistants=lambda _team_id: (hosted_active,),
                _model_credential=lambda _owner, _provider, *_args: ("test-key", 7),
                _require_model_credential_current=lambda *_args: hosted_events.append("model"),
                _require_hosted_power_rpc_envelope=lambda *_args: hosted_events.append("preflight"),
                _hosted_power_identity=lambda _active: (assistant_container.id, local_spec.image),
                _power_integration_generations=lambda *_args: hosted_events.append("integrations") or (),
            ),
            mock.patch.object(
                hosted_harness.assistant_lifecycle,
                "_require_assistant_genesis",
                return_value="Use the declared Power.",
            ),
            mock.patch.object(
                hosted_harness.hosted_chat_segment,
                "_hosted_private_requirements",
                return_value=("integration-required",),
            ),
            mock.patch.object(
                hosted_harness.runtime_state,
                "_inference_store",
                SimpleNamespace(load=lambda _team_id: config),
            ),
            mock.patch.object(
                hosted_harness.runtime_state,
                "_storage",
                side_effect=lambda: SimpleNamespace(metadata=lambda _team_id, _files, *_args: []),
            ),
            mock.patch.object(
                hosted_harness.hosted_chat_segment.chat_turn_engine,
                "run_segment",
                side_effect=capture("hosted"),
            ),
        ):
            hosted_harness.hosted_chat_segment._run_hosted_chat_segment(
                hosted_harness.hosted_chat_segment.HostedChatSegmentRequest(
                    team_id="team_1",
                    file_ids=[],
                    assistant_ids=(assistant_id,),
                    token=hosted_token,
                    container=hosted_anchor,
                    owner="owner",
                    message="Look this up",
                )
            )
            hosted_strategy, hosted_prepared, _, _ = captures["hosted"]
            hosted_validation_result = hosted_strategy.validate_power(assistant_id, "list-zones", request.input)
            hosted_prepared.durable_batch._operation(request)
            hosted_strategy.finalize()

        local_events: list[str] = []
        controller = _local_controller(local_active, config, local_events, self.fail)
        turn_token = "turn-token"

        with mock.patch.object(local_chat_segment.chat_turn_engine, "run_segment", side_effect=capture("local")):
            controller.chat_turn_service._run_chat_segment(
                SegmentRequest(
                    team_id="team_1",
                    file_ids=[],
                    assistant_ids=(assistant_id,),
                    provider="openai",
                    api_key="test-key",
                    token=turn_token,
                    message="Look this up",
                )
            )

        hosted_strategy, hosted_prepared, hosted_requirements, hosted_paused = captures["hosted"]
        local_strategy, local_prepared, local_requirements, local_paused = captures["local"]

        self.assertEqual(_context_contract(hosted_prepared), _context_contract(local_prepared))
        self.assertEqual(hosted_requirements.groups(), local_requirements.groups())
        self.assertTrue(hosted_paused)
        self.assertTrue(local_paused)
        self.assertEqual(
            hosted_validation_result,
            local_strategy.validate_power(assistant_id, "list-zones", request.input),
        )

        local_prepared.durable_batch._operation(request)
        local_strategy.finalize()
        self.assertEqual(hosted_events, ["preflight", "integrations"])
        self.assertEqual(local_events, ["preflight", "integrations"])


if __name__ == "__main__":
    unittest.main()
