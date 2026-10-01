from __future__ import annotations

import dataclasses
import sys
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from action import challenges as action_challenges
from action import human as action_human
from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from inference import config as inference_config
from integrations import challenges as integration_challenges
from local.chat import continuation as local_chat_continuations
from local.chat import continuation_store as local_chat_continuation_store

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
                }
            ],
            inference_config.normalize("openai", "gpt-6-luna"),
        ),
    )


class LocalChatContinuationCodecTests(unittest.TestCase):
    def _round_trip(
        self,
        kind: str,
        requirements: tuple[object, ...],
        state: local_chat_continuations.PendingLocalChat | None = None,
    ) -> None:
        state = pending() if state is None else state
        bindings, payload = local_chat_continuations.encode(kind, requirements, state)
        stored = local_chat_continuation_store.StoredContinuation(
            "team_1",
            kind,
            "c" * 32,
            1_300,
            1,
            bindings,
            payload,
        )
        decoded = local_chat_continuations.decode(stored)
        self.assertEqual(decoded.kind, kind)
        self.assertEqual(decoded.requirements, requirements)
        self.assertEqual(decoded.pending, state)
        # A frozen Routine run keeps the same parts in its own store and decodes them identically.
        self.assertEqual(local_chat_continuations.decode_parts(kind, payload, bindings), decoded)

    def test_rejects_an_invoked_action_whose_skill_structure_is_malformed(self) -> None:
        requirements = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant", "Demo Assistant", ("publish",), (("cloudflare", "cloudflare", ("dns.read",)),)
            ),
        )
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
        requirements = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant",
                "Demo Assistant",
                ("publish",),
                (("cloudflare", "cloudflare", ("dns.read", "zone.read")),),
            ),
        )
        self._round_trip("integrations", requirements)

    def test_round_trips_a_local_snapshot_integration_suspension(self) -> None:
        requirements = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant",
                "Demo Assistant",
                ("publish",),
                (("cloudflare", "cloudflare", ("dns.read", "zone.read")),),
            ),
        )
        state = pending(LOCAL_IMAGE)
        bindings, payload = local_chat_continuations.encode("integrations", requirements, state)
        decoded = local_chat_continuations.decode(
            local_chat_continuation_store.StoredContinuation(
                "team_1",
                "integrations",
                "c" * 32,
                1_300,
                1,
                bindings,
                payload,
            )
        )

        self.assertEqual(decoded.requirements, requirements)
        self.assertEqual(decoded.pending, state)
        self.assertEqual(bindings, (f"demo-assistant/publish/{LOCAL_IMAGE}/-",))

    def test_round_trips_every_team_name_and_filename_their_owners_admit(self) -> None:
        requirements = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant", "Demo Assistant", ("publish",), (("cloudflare", "cloudflare", ("dns.read",)),)
            ),
        )
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467"
        for team_name, filename in (
            ("Research Team", "meeting notes.txt"),
            (f"Research\u00a0Team {family}", f"meeting\u00a0notes {family}.txt"),
            ("x" * 80, "\u00e9" * 127 + "x"),
        ):
            with self.subTest(team_name=team_name, filename=filename):
                self._round_trip("integrations", requirements, pending(team_name=team_name, filename=filename))

    def test_rejects_team_names_and_filenames_their_owners_refuse(self) -> None:
        requirements = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant", "Demo Assistant", ("publish",), (("cloudflare", "cloudflare", ("dns.read",)),)
            ),
        )
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
        requirements = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant",
                "Demo Assistant",
                ("publish",),
                (("cloudflare", "cloudflare", ("dns.read",)),),
            ),
        )
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
            request["fingerprint"] = action_human._fingerprint(request)
            admitted = action_human.validate_request(request, (request["kind"],), declared)
            with self.subTest(kind=request["kind"], stored_input=stored_input):
                self._round_trip(
                    "human",
                    (
                        action_challenges.HumanRequirement(
                            "demo-assistant",
                            "Demo Assistant",
                            "publish",
                            "Publish.",
                            "action-1",
                            admitted,
                            "0.4.1",
                        ),
                    ),
                )

    def test_encoding_refuses_a_continuation_that_would_not_restore(self) -> None:
        requirements = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant", "Demo Assistant", ("publish",), (("cloudflare", "cloudflare", ("dns.read",)),)
            ),
        )
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
        request["fingerprint"] = action_human._fingerprint(request)
        requirement = (
            action_challenges.HumanRequirement(
                "demo-assistant",
                "Demo Assistant",
                "publish",
                "Search the web.",
                "action-1",
                action_human.validate_request(request, ("input:password",), ("exa-api-key",)),
                "0.4.1",
            ),
        )
        self._round_trip("human", requirement)
        self.assertEqual(requirement[0].request.stored_input, "exa-api-key")

    def test_round_trips_human_suspension_and_nonsecret_transcript(self) -> None:
        first = {
            "kind": "approval",
            "ordinal": 0,
            "title": "Prepare",
            "description": "Prepare the reviewed action.",
        }
        first["fingerprint"] = action_human._fingerprint(first)
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
        current["fingerprint"] = action_human._fingerprint(current)
        state = pending()
        state = local_chat_continuations.PendingLocalChat(
            state.continuation,
            state.assistant_ids,
            state.file_ids,
            state.provider,
            state.identity,
            (
                action_human.ActionTranscript(
                    "action-1",
                    (action_human.admit_response(action_human.validate_request(first, ("approval",)), True),),
                ),
            ),
            1,
        )
        requirement = (
            action_challenges.HumanRequirement(
                "demo-assistant",
                "Demo Assistant",
                "publish",
                "Publish a DNS record.",
                "action-1",
                action_human.validate_request(current, ("input:text",)),
                "0.4.1",
            ),
        )

        bindings, payload = local_chat_continuations.encode("human", requirement, state)
        decoded = local_chat_continuations.decode(
            local_chat_continuation_store.StoredContinuation("team_1", "human", "c" * 32, 1_300, 1, bindings, payload)
        )

        self.assertEqual(decoded.requirements, requirement)
        self.assertEqual(decoded.pending, state)

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
        secret_request["fingerprint"] = action_human._fingerprint(secret_request)
        state = pending()
        state = local_chat_continuations.PendingLocalChat(
            state.continuation,
            state.assistant_ids,
            state.file_ids,
            state.provider,
            state.identity,
            (
                action_human.ActionTranscript(
                    "action-1",
                    (
                        action_human.admit_response(
                            action_human.validate_request(secret_request, ("input:password",)), "secret"
                        ),
                    ),
                ),
            ),
            1,
        )

        with self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "secret"):
            local_chat_continuations.encode(
                "human",
                (
                    action_challenges.HumanRequirement(
                        "demo-assistant",
                        "Demo Assistant",
                        "publish",
                        "Publish a DNS record.",
                        "action-1",
                        action_human.validate_request(secret_request, ("input:password",)),
                        "0.4.1",
                    ),
                ),
                state,
            )

    def test_restart_preserves_the_monotonic_human_request_budget(self) -> None:
        requirement = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant",
                "Demo Assistant",
                ("publish",),
                (("cloudflare", "cloudflare", ("dns.read",)),),
            ),
        )
        state = replace(pending(), requests_used=action_human.MAX_REQUESTS_PER_TURN)
        bindings, payload = local_chat_continuations.encode("integrations", requirement, state)
        decoded = local_chat_continuations.decode(
            local_chat_continuation_store.StoredContinuation(
                "team_1",
                "integrations",
                "c" * 32,
                1_300,
                1,
                bindings,
                payload,
            )
        )

        self.assertEqual(decoded.pending.requests_used, action_human.MAX_REQUESTS_PER_TURN)

    def test_rejects_release_binding_and_decrypted_shape_drift(self) -> None:
        requirement = (
            integration_challenges.IntegrationRequirement(
                "demo-assistant",
                "Demo Assistant",
                ("publish",),
                (("cloudflare", "cloudflare", ("dns.read", "zone.read")),),
            ),
        )
        bindings, payload = local_chat_continuations.encode("integrations", requirement, pending())
        drifted = local_chat_continuation_store.StoredContinuation(
            "team_1",
            "integrations",
            "c" * 32,
            1_300,
            1,
            ("demo-assistant/publish/" + IMAGE + "/changed",),
            payload,
        )
        with self.assertRaisesRegex(
            local_chat_continuations.ContinuationCodecError,
            "binding changed",
        ):
            local_chat_continuations.decode(drifted)

        malformed = local_chat_continuation_store.StoredContinuation(
            "team_1",
            "integrations",
            "c" * 32,
            1_300,
            1,
            bindings,
            b'{"schema":1,"kind":"integrations","requirements":[],"pending":{}}',
        )
        with self.assertRaises(local_chat_continuations.ContinuationCodecError):
            local_chat_continuations.decode(malformed)


if __name__ == "__main__":
    unittest.main()
