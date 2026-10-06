from __future__ import annotations

import base64
import dataclasses
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from action import challenges as action_challenges
from action import human as action_human
from assistant import action_schema
from assistant import spec as assistant_spec
from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from inference import config as inference_config
from inference import usage as brain_usage
from integrations import challenges as integration_challenges
from local.chat import continuation as local_chat_continuations
from local.chat import continuation_store as local_chat_continuation_store
from local.routine import store as routine_store
from routine import permission as routine_permission
from routine import plan as routine_plan
from tests import human_request_fixtures

IMAGE = "registry.example/assistant@sha256:" + "b" * 64
LOCAL_IMAGE = "sha256:" + "c" * 64
TURN = brain_runtime_client.RuntimeTurn(
    status="action-required",
    reply="",
    actions=(
        brain_runtime_client.ActionRequest(
            interrupt_id="action-1",
            assistant_id="demo-assistant",
            action="publish",
            input={"message": "private Action input"},
        ),
    ),
)


def pending(
    image: str = IMAGE, *, team_name: str = "Demo Team", filename: str = "brief.txt"
) -> local_chat_continuations.PendingLocalChat:
    return local_chat_continuations.PendingLocalChat(
        continuation=chat_orchestrator.ChatContinuation(
            turn=TURN,
            seen_interrupts=("older-action",),
            invoked=(chat_orchestrator.InvokedAction("demo-assistant", "lookup", ("query",), "sha256:" + "d" * 64),),
            round_index=1,
        ),
        assistant_ids=("demo-assistant",),
        file_ids=("a" * 32,),
        provider="openai",
        identity=(
            team_name,
            "network-id",
            (("demo-assistant", image, "container-id"),),
            [
                {
                    "id": "a" * 32,
                    "name": filename,
                    "media_type": "text/plain",
                    "size": 42,
                    "sha256": "b" * 64,
                }
            ],
            inference_config.normalize("openai", "gpt-6-luna"),
        ),
    )


# The fingerprint of the Action batch a human request paused; an Integration pause holds none.
PAUSED_BATCH = "e" * 64


def human_pending(**changes: object) -> local_chat_continuations.PendingLocalChat:
    return replace(pending(), paused_batch=PAUSED_BATCH, **changes)


def integration_requirement(*scopes: str) -> tuple[integration_challenges.IntegrationRequirement, ...]:
    """The demo Assistant's paused Cloudflare Integration requirement over exactly these scopes."""
    return (
        integration_challenges.IntegrationRequirement(
            "demo-assistant", "Demo Assistant", ("publish",), (("cloudflare", "cloudflare", scopes),)
        ),
    )


def stored_record(
    kind: str, bindings: tuple[str, ...], payload: bytes
) -> local_chat_continuation_store.StoredContinuation:
    return local_chat_continuation_store.StoredContinuation("team_1", kind, "c" * 32, 1_300, 1, bindings, payload)


def human_requirement(
    request: dict[str, object], kinds: tuple[str, ...], declared: tuple[str, ...] = (), *, purpose: str
) -> action_challenges.HumanRequirement:
    """A paused human requirement whose admitted request and localized copy come from the same fixture."""
    admitted = human_request_fixtures.admit(human_request_fixtures.fingerprinted(request), kinds, declared)
    return action_challenges.HumanRequirement(
        "demo-assistant",
        "Demo Assistant",
        "publish",
        purpose,
        "action-1",
        admitted,
        "0.4.1",
        copy=human_request_fixtures.copy(admitted),
    )


class LocalChatContinuationCodecTests(unittest.TestCase):
    def _round_trip(
        self,
        kind: str,
        requirements: tuple[object, ...],
        state: local_chat_continuations.PendingLocalChat | None = None,
    ) -> None:
        if state is None:
            state = human_pending() if kind == "human" else pending()
        bindings, payload = local_chat_continuations.encode(kind, requirements, state)
        decoded = local_chat_continuations.decode(stored_record(kind, bindings, payload))
        self.assertEqual(decoded.kind, kind)
        self.assertEqual(decoded.requirements, requirements)
        self.assertEqual(decoded.pending, state)
        # A frozen Routine run keeps the same parts in its own store and decodes them identically.
        self.assertEqual(local_chat_continuations.decode_parts(kind, payload, bindings), decoded)

    def test_rejects_an_invoked_action_whose_skill_structure_is_malformed(self) -> None:
        requirements = integration_requirement("dns.read")
        for invoked in (
            chat_orchestrator.InvokedAction("demo-assistant", "lookup", ("b", "a"), "sha256:" + "d" * 64),
            chat_orchestrator.InvokedAction("demo-assistant", "lookup", ("query",), "not-a-digest"),
            chat_orchestrator.InvokedAction("demo-assistant", "lookup", ("query",), "sha256:" + "d" * 64, "yes"),
        ):
            base = pending()
            broken = dataclasses.replace(base, continuation=dataclasses.replace(base.continuation, invoked=(invoked,)))
            with (
                self.subTest(invoked=invoked),
                self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "invoked Action is malformed"),
            ):
                # Encoding verifies its own round trip, so a malformed invoked Action never reaches storage.
                local_chat_continuations.encode("integrations", requirements, broken)

    def test_round_trips_the_integration_suspension(self) -> None:
        requirements = integration_requirement("dns.read", "zone.read")
        self._round_trip("integrations", requirements)

    def test_every_admitted_action_input_survives_a_pause(self) -> None:
        requirements = integration_requirement("dns.read")
        action = assistant_spec.ActionSpec(
            "Publish",
            action_schema.admitted(
                {
                    "type": "object",
                    "properties": {"x": {"type": "array"}},
                    "patternProperties": {"^k+$": {"type": "integer"}},
                    "additionalProperties": False,
                }
            ),
            {"type": "object", "additionalProperties": False},
        )

        def nested(levels: int) -> object:
            value: object = 0
            for _index in range(levels):
                value = [value]
            return value

        def paused(action_input: dict[str, object]) -> local_chat_continuations.PendingLocalChat:
            base = pending()
            request = replace(TURN.actions[0], input=action_input)
            return replace(base, continuation=replace(base.continuation, turn=replace(TURN, actions=(request,))))

        # Each once failed to persist after admission, which cancelled the challenge and purged the Action journal.
        admitted_inputs = (
            {"k" * 129: 1},
            {"x": nested(17)},
            {"x": list(range(5_000))},
            # The value under "x" sits one level below the input, so this reaches exactly the shared depth bound.
            {"x": nested(action_schema.MAX_PAYLOAD_DEPTH - 1)},
        )
        for action_input in admitted_inputs:
            with self.subTest(size=len(str(action_input))):
                self.assertEqual(assistant_spec.validate_action_payload(action, "input", action_input), action_input)
                self._round_trip("integrations", requirements, paused(action_input))

        # One level deeper is refused at admission, before any challenge exists, under the bound the codec applies.
        too_deep = {"x": nested(action_schema.MAX_PAYLOAD_DEPTH)}
        with self.assertRaisesRegex(ValueError, "nests too deeply"):
            assistant_spec.validate_action_payload(action, "input", too_deep)
        with self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "structure limit"):
            local_chat_continuations.encode("integrations", requirements, paused(too_deep))

    def test_round_trips_a_local_snapshot_integration_suspension(self) -> None:
        requirements = integration_requirement("dns.read", "zone.read")
        state = pending(LOCAL_IMAGE)
        bindings, payload = local_chat_continuations.encode("integrations", requirements, state)
        decoded = local_chat_continuations.decode(stored_record("integrations", bindings, payload))

        self.assertEqual(decoded.requirements, requirements)
        self.assertEqual(decoded.pending, state)
        self.assertEqual(bindings, (f"demo-assistant/publish/{LOCAL_IMAGE}/-",))

    def test_round_trips_every_team_name_and_filename_their_owners_admit(self) -> None:
        requirements = integration_requirement("dns.read")
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        for team_name, filename in (
            ("Research Team", "meeting notes.txt"),
            (f"Research\u00a0Team {family}", f"meeting\u00a0notes {family}.txt"),
            ("x" * 80, "\u00e9" * 127 + "x"),
        ):
            with self.subTest(team_name=team_name, filename=filename):
                self._round_trip("integrations", requirements, pending(team_name=team_name, filename=filename))

    def test_rejects_team_names_and_filenames_their_owners_refuse(self) -> None:
        requirements = integration_requirement("dns.read")
        for team_name in ("", " Research Team", "Research\nTeam", "x" * 81):
            with (
                self.subTest(team_name=team_name),
                self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "Team name is malformed"),
            ):
                local_chat_continuations.encode("integrations", requirements, pending(team_name=team_name))
        for filename in ("", "..", "notes/brief.txt", "notes\\brief.txt", "brief\x7f.txt", "\u00e9" * 128):
            with (
                self.subTest(filename=filename),
                self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "file is malformed"),
            ):
                local_chat_continuations.encode("integrations", requirements, pending(filename=filename))

    def test_rejects_mutable_or_malformed_image_identities(self) -> None:
        requirements = integration_requirement("dns.read")
        invalid = (
            "registry.example/assistant:latest",
            "registry.example/assistant",
            "@sha256:" + "a" * 64,
            "sha256:" + "A" * 64,
            "sha256:" + "a" * 63,
            "sha256:" + "a" * 65,
            "a" * 64,
        )
        for image in invalid:
            with (
                self.subTest(image=image),
                self.assertRaisesRegex(
                    local_chat_continuations.ContinuationCodecError,
                    "release is malformed",
                ),
            ):
                local_chat_continuations.encode("integrations", requirements, pending(image))

    def test_every_admitted_human_request_kind_round_trips(self) -> None:
        options = [
            {"value": "safe", "label": "Safe", "description": None},
            {"value": "fast", "label": "Fast", "description": "Apply all."},
        ]
        base = {"ordinal": 0, "title": "Continue", "description": "Continue this Action."}
        text = {"label": "Value", "required": True, "placeholder": None, "min_length": 1}
        shapes = [({"kind": kind}, (), None) for kind in ("approval", *sorted(action_human.AUTH_KINDS))]
        shapes += [
            ({"kind": kind, **text, "max_length": limit}, (), None)
            for kind, limit in sorted(action_human.LENGTH_KINDS.items())
        ]
        shapes.append(
            (
                {"kind": "input:password", **text, "max_length": 256, "stored_input": "exa-api-key"},
                ("exa-api-key",),
                "exa-api-key",
            )
        )
        shapes += [
            ({"kind": kind, "label": "Mode", "required": True, "options": options}, (), None)
            for kind in sorted(action_human.CHOICE_KINDS)
        ]
        shapes.append(
            (
                {
                    "kind": "input:choices",
                    "label": "Modes",
                    "required": True,
                    "options": options,
                    "min_selections": 1,
                    "max_selections": 2,
                },
                (),
                None,
            )
        )
        for fields, declared, stored_input in shapes:
            request = {**base, **fields}
            with self.subTest(kind=request["kind"], stored_input=stored_input):
                self._round_trip(
                    "human", (human_requirement(request, (request["kind"],), declared, purpose="Publish."),)
                )

    def test_encoding_refuses_a_continuation_that_would_not_restore(self) -> None:
        requirements = integration_requirement("dns.read")
        drifted = local_chat_continuations.DecodedContinuation(
            "integrations", requirements, replace(pending(), provider="anthropic")
        )
        with (
            mock.patch.object(local_chat_continuations, "_decoded", return_value=drifted),
            self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "does not round-trip"),
        ):
            local_chat_continuations.encode("integrations", requirements, pending())

    def test_round_trips_a_pending_stored_input_request(self) -> None:
        request = {
            "kind": "input:password",
            "ordinal": 0,
            "title": "Exa API key",
            "description": "Paste the Exa API key once.",
            "label": "API key",
            "required": True,
            "placeholder": None,
            "min_length": 1,
            "max_length": 1024,
            "stored_input": "exa-api-key",
        }
        requirement = (human_requirement(request, ("input:password",), ("exa-api-key",), purpose="Search the web."),)
        self._round_trip("human", requirement)
        self.assertEqual(requirement[0].request.stored_input, "exa-api-key")

    def test_round_trips_human_suspension_and_nonsecret_transcript(self) -> None:
        first = {
            "kind": "approval",
            "ordinal": 0,
            "title": "Prepare",
            "description": "Prepare the reviewed action.",
        }
        current = {
            "kind": "input:text",
            "ordinal": 1,
            "title": "Zone",
            "description": "Enter the reviewed zone.",
            "label": "Zone",
            "required": True,
            "placeholder": "example.com",
            "min_length": 1,
            "max_length": 255,
        }
        answered = action_human.admit_response(
            human_request_fixtures.admit(human_request_fixtures.fingerprinted(first), ("approval",)), True
        )
        state = human_pending(transcripts=(action_human.ActionTranscript("action-1", (answered,)),), requests_used=1)
        requirement = (human_requirement(current, ("input:text",), purpose="Publish a DNS record."),)

        bindings, payload = local_chat_continuations.encode("human", requirement, state)
        decoded = local_chat_continuations.decode(stored_record("human", bindings, payload))

        self.assertEqual(decoded.requirements, requirement)
        self.assertEqual(decoded.pending, state)

    def test_schema_seven_keeps_the_paused_batch_turn_locale_usage_localized_copy_and_file_digests_together(
        self,
    ) -> None:
        """One paused record carries its Action batch, ADR-0091 locale and copy, ADR-0082 usage, and file digests."""
        request = human_request_fixtures.request("approval")
        requirement = (
            human_request_fixtures.requirement(
                request, locale="pt", assistant_id="demo-assistant", purpose="Publish it.", purpose_locale="pt"
            ),
        )
        usage = brain_usage.TurnUsage(1_700_000_000_000, (("openai", "gpt-6-luna", 1331, 36),))
        state = human_pending(locale="pt", usage=usage)

        bindings, payload = local_chat_continuations.encode("human", requirement, state)
        decoded = local_chat_continuations.decode(stored_record("human", bindings, payload))
        self.assertEqual(local_chat_continuations.SCHEMA_VERSION, 7)
        self.assertEqual(decoded.pending.paused_batch, PAUSED_BATCH)
        self.assertEqual(decoded.pending.identity[3][0]["sha256"], "b" * 64)
        self.assertEqual(decoded.pending, state)
        self.assertEqual(decoded.requirements, requirement)
        self.assertEqual(decoded.requirements[0].copy.locale, "pt")

        body = json.loads(payload)
        variants = {
            "schema 6": {**body, "schema": 6},
            "no paused batch": {**body, "pending": {k: v for k, v in body["pending"].items() if k != "paused_batch"}},
            "human without a paused batch": {**body, "pending": {**body["pending"], "paused_batch": None}},
            "short paused batch": {**body, "pending": {**body["pending"], "paused_batch": "e" * 63}},
            "uppercase paused batch": {**body, "pending": {**body["pending"], "paused_batch": "E" * 64}},
            "no locale": {**body, "pending": {k: v for k, v in body["pending"].items() if k != "locale"}},
            "no usage": {**body, "pending": {k: v for k, v in body["pending"].items() if k != "usage"}},
        }
        for name, variant in variants.items():
            with self.subTest(name), self.assertRaises(local_chat_continuations.ContinuationCodecError):
                local_chat_continuations.decode(stored_record("human", bindings, json.dumps(variant).encode()))

    def test_refuses_to_persist_password_response_material(self) -> None:
        secret_request = {
            "kind": "input:password",
            "ordinal": 0,
            "title": "Provider secret",
            "description": "Enter the third-party provider secret.",
            "label": "Secret",
            "required": True,
            "placeholder": None,
            "min_length": 1,
            "max_length": 64,
        }
        answered = action_human.admit_response(
            human_request_fixtures.admit(human_request_fixtures.fingerprinted(secret_request), ("input:password",)),
            "secret",
        )
        state = human_pending(transcripts=(action_human.ActionTranscript("action-1", (answered,)),), requests_used=1)

        with self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "secret"):
            local_chat_continuations.encode(
                "human",
                (human_requirement(secret_request, ("input:password",), purpose="Publish a DNS record."),),
                state,
            )

    def test_only_a_human_pause_names_its_paused_action_batch(self) -> None:
        integration = integration_requirement("dns.read")
        human = (
            human_request_fixtures.requirement(
                human_request_fixtures.request("approval"), locale="en", assistant_id="demo-assistant"
            ),
        )
        for kind, requirements, state in (
            ("integrations", integration, human_pending()),
            ("human", human, pending()),
        ):
            with (
                self.subTest(kind=kind),
                self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "paused Action batch"),
            ):
                local_chat_continuations.encode(kind, requirements, state)

    def test_a_recording_turn_keeps_only_its_recording_id_across_a_pause(self) -> None:
        requirement = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant", "Demo Assistant", ("publish",), (("cloudflare", "cloudflare", ("dns.read",)),)
            ),
        )
        recorded = dataclasses.replace(pending(), recording="b" * 32)
        bindings, payload = local_chat_continuations.encode("integrations", requirement, recorded)
        decoded = local_chat_continuations.decode_parts("integrations", payload, bindings)
        self.assertEqual(decoded.pending.recording, "b" * 32)
        for value in ("not-a-recording", 7):
            document = json.loads(payload)
            document["pending"]["recording"] = value
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "pending recording"),
            ):
                local_chat_continuations.decode_parts("integrations", json.dumps(document).encode(), bindings)

    def test_a_paused_turn_keeps_only_a_model_of_its_provider(self) -> None:
        requirement = integration_requirement("dns.read")
        for model in (None, "gpt-6.1-sol"):
            bindings, payload = local_chat_continuations.encode(
                "integrations", requirement, dataclasses.replace(pending(), model=model)
            )
            decoded = local_chat_continuations.decode_parts("integrations", payload, bindings)
            self.assertEqual(decoded.pending.model, model)
        for value in ("claude-sonnet-5-5", "gpt-unknown", 7, ["gpt-6.1-sol"]):
            document = json.loads(payload)
            document["pending"]["model"] = value
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "pending model"),
            ):
                local_chat_continuations.decode_parts("integrations", json.dumps(document).encode(), bindings)

    def test_restart_preserves_the_monotonic_human_request_budget(self) -> None:
        requirement = integration_requirement("dns.read")
        state = replace(pending(), requests_used=action_human.MAX_REQUESTS_PER_TURN)
        bindings, payload = local_chat_continuations.encode("integrations", requirement, state)
        decoded = local_chat_continuations.decode(stored_record("integrations", bindings, payload))

        self.assertEqual(decoded.pending.requests_used, action_human.MAX_REQUESTS_PER_TURN)

    def test_rejects_release_binding_and_decrypted_shape_drift(self) -> None:
        requirement = integration_requirement("dns.read", "zone.read")
        bindings, payload = local_chat_continuations.encode("integrations", requirement, pending())
        drifted = stored_record("integrations", ("demo-assistant/publish/" + IMAGE + "/changed",), payload)
        with self.assertRaisesRegex(
            local_chat_continuations.ContinuationCodecError,
            "binding changed",
        ):
            local_chat_continuations.decode(drifted)

        malformed = stored_record(
            "integrations", bindings, b'{"schema":1,"kind":"integrations","requirements":[],"pending":{}}'
        )
        with self.assertRaises(local_chat_continuations.ContinuationCodecError):
            local_chat_continuations.decode(malformed)


if __name__ == "__main__":
    unittest.main()


def _longest_answers() -> tuple[action_human.HumanResponse, ...]:
    """An unfinished Action's earlier answers at their longest, each control character escaped to six bytes."""
    longest = max(action_human.LENGTH_KINDS.values())
    return tuple(
        action_human.HumanResponse("input:textarea", ordinal, "f" * 64, "\x02" * longest)
        for ordinal in range(action_human.MAX_REQUESTS_PER_ACTION - 1)
    )


def _longest_plan_pending(
    turn: brain_runtime_client.RuntimeTurn,
    transcript: str,
    answers: tuple[action_human.HumanResponse, ...],
    **changes: object,
) -> local_chat_continuations.PendingLocalChat:
    """A paused Routine turn at the last step of the longest plan, every earlier step already seen."""
    steps = routine_plan.MAX_STEPS
    return local_chat_continuations.PendingLocalChat(
        chat_orchestrator.ChatContinuation(
            turn=turn,
            seen_interrupts=tuple(sorted(f"routine-step-{index}" for index in range(steps - 1))),
            invoked=(),
            round_index=steps - 1,
        ),
        ("demo-assistant",),
        (),
        "openai",
        (*pending().identity[:3], [], pending().identity[4]),
        (action_human.ActionTranscript(transcript, answers),),
        len(answers),
        **changes,
    )


def _routine_record(kind: str, bindings: tuple[str, ...], payload: bytes) -> bytes:
    """The continuation record a frozen Routine run stores for an encoded continuation."""
    return json.dumps(
        {"kind": kind, "bindings": list(bindings), "payload": base64.b64encode(payload).decode("ascii")},
        separators=(",", ":"),
    ).encode("ascii")


def _sealed_and_read(blob: bytes) -> bytes | None:
    """Seal a continuation record in a fresh Routine store and read it back."""
    with tempfile.TemporaryDirectory() as directory:
        store = routine_store.RoutineStore(Path(directory) / "state", Path(directory) / "key" / "aes256.key")
        store.put_continuation("team_1", "d" * 32, blob)
        return store.continuation("team_1", "d" * 32)


class RoutineContinuationBoundTests(unittest.TestCase):
    """A frozen Routine run's continuation fits its own derived bounds at every field's worst (scale)."""

    def test_the_largest_frozen_step_of_the_longest_plan_fits_its_codec_record_and_seal(self) -> None:
        steps = routine_plan.MAX_STEPS
        # The pending request's resolved input at its canonical bound, every character escaped to six bytes.
        filler = "\x01" * ((routine_plan.MAX_RESOLVED_INPUT_BYTES - 32) // 6)
        resolved = {"payload": filler}
        self.assertLessEqual(len(routine_plan.canonical(resolved)), routine_plan.MAX_RESOLVED_INPUT_BYTES)
        interrupt = f"routine-step-{steps - 1}"
        turn = brain_runtime_client.RuntimeTurn(
            "action-required",
            "",
            (brain_runtime_client.ActionRequest(interrupt, "demo-assistant", "publish", resolved),),
        )
        answers = _longest_answers()
        state = _longest_plan_pending(turn, interrupt, answers, paused_batch=PAUSED_BATCH)
        request = human_request_fixtures.request("approval", len(answers))
        requirement = (
            human_request_fixtures.requirement(request, interrupt_id=interrupt, assistant_id="demo-assistant"),
        )
        with self.assertRaises(local_chat_continuations.ContinuationCodecError):
            local_chat_continuations.encode("human", requirement, state)
        bindings, payload = local_chat_continuations.encode(
            "human", requirement, state, limit=local_chat_continuations.MAX_ROUTINE_PLAINTEXT_BYTES
        )
        self.assertEqual(local_chat_continuations.decode_parts("human", payload, bindings).pending, state)
        blob = _routine_record("human", bindings, payload)
        self.assertLessEqual(len(blob), local_chat_continuations.MAX_ROUTINE_BYTES)
        self.assertEqual(_sealed_and_read(blob), blob)


def permission_requirement(**changes: object) -> routine_permission.Requirement:
    """A decision call frozen for a person's Permitir (ADR-0101 section 8), bound to the turn's one pending Action."""
    return replace(
        routine_permission.Requirement(
            run_id="d" * 32,
            revision=2,
            permissions_revision=0,
            interrupt_id="action-1",
            operation_id="6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
            assistant="demo-assistant",
            action="publish",
            pin="sha256:" + "a" * 64,
            commitment="e" * 64,
        ),
        **changes,
    )


class PermissionContinuationTests(unittest.TestCase):
    """A frozen decision call's permission continuation, which only a frozen Routine run's store keeps."""

    def test_a_permission_pause_round_trips_bound_to_its_pending_action(self) -> None:
        requirement = (permission_requirement(),)
        bindings, payload = local_chat_continuations.encode("permission", requirement, pending())
        self.assertEqual(bindings, (f"demo-assistant/publish/{IMAGE}/{'e' * 64}",))
        decoded = local_chat_continuations.decode_parts("permission", payload, bindings)
        self.assertEqual((decoded.kind, decoded.requirements, decoded.pending), ("permission", requirement, pending()))
        # A chat turn never waits for a Routine permission, so the chat store refuses the kind.
        with self.assertRaises(local_chat_continuation_store.ContinuationStoreError):
            local_chat_continuation_store._kind("permission")

    def test_a_permission_pause_holds_one_bound_requirement_and_no_action_batch(self) -> None:
        requirement = permission_requirement()
        for requirements, state in (
            ((requirement, requirement), pending()),
            ((), pending()),
            ((object(),), pending()),
            ((requirement,), human_pending()),
            ((permission_requirement(interrupt_id="older-action"),), pending()),
            ((permission_requirement(interrupt_id="action-2"),), pending()),
            ((permission_requirement(assistant="other-assistant"),), pending()),
            ((permission_requirement(action="lookup"),), pending()),
        ):
            with (
                self.subTest(requirements=requirements),
                self.assertRaises(local_chat_continuations.ContinuationCodecError),
            ):
                local_chat_continuations.encode("permission", requirements, state)

    def test_every_malformed_permission_field_is_refused_on_decode(self) -> None:
        bindings, payload = local_chat_continuations.encode("permission", (permission_requirement(),), pending())
        for field, value in (
            ("run_id", "D" * 32),
            ("revision", 0),
            ("revision", True),
            ("revision", 2**31),
            ("permissions_revision", -1),
            ("permissions_revision", False),
            ("permissions_revision", 2**31),
            ("permissions_revision", 1.0),
            ("interrupt_id", "bad interrupt"),
            ("operation_id", "not-an-operation"),
            ("assistant", "Bad Assistant"),
            ("action", "Bad Action"),
            ("pin", "a" * 64),
            ("commitment", "sha256:" + "e" * 64),
            ("extra", None),
        ):
            document = json.loads(payload)
            document["requirements"][0][field] = value
            with (
                self.subTest(field=field, value=value),
                self.assertRaises(local_chat_continuations.ContinuationCodecError),
            ):
                local_chat_continuations.decode_parts("permission", json.dumps(document).encode(), bindings)
        document = json.loads(payload)
        document["requirements"].append(document["requirements"][0])
        with self.assertRaises(local_chat_continuations.ContinuationCodecError):
            local_chat_continuations.decode_parts("permission", json.dumps(document).encode(), bindings)
        with self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "release binding changed"):
            local_chat_continuations.decode_parts("permission", payload, (f"demo-assistant/publish/{IMAGE}/-",))

    def test_the_largest_permission_freeze_of_the_longest_plan_fits_its_codec_record_and_seal(self) -> None:
        filler = "\x01" * ((routine_plan.MAX_RESOLVED_INPUT_BYTES - 32) // 6)
        interrupt = "i" * 256
        turn = brain_runtime_client.RuntimeTurn(
            "action-required",
            "",
            (brain_runtime_client.ActionRequest(interrupt, "demo-assistant", "publish", {"payload": filler}),),
        )
        answers = _longest_answers()
        state = _longest_plan_pending(turn, "routine-step-0", answers)
        requirement = (
            permission_requirement(
                interrupt_id=interrupt, revision=2**31 - 1, permissions_revision=2**31 - 1, run_id="f" * 32
            ),
        )
        with self.assertRaises(local_chat_continuations.ContinuationCodecError):
            local_chat_continuations.encode("permission", requirement, state)
        bindings, payload = local_chat_continuations.encode(
            "permission", requirement, state, limit=local_chat_continuations.MAX_ROUTINE_PLAINTEXT_BYTES
        )
        decoded = local_chat_continuations.decode_parts("permission", payload, bindings)
        self.assertEqual((decoded.requirements, decoded.pending), (requirement, state))
        blob = _routine_record("permission", bindings, payload)
        self.assertLessEqual(len(blob), local_chat_continuations.MAX_ROUTINE_BYTES)
        self.assertEqual(_sealed_and_read(blob), blob)
