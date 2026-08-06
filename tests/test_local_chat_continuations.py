from __future__ import annotations

import sys
import unittest
from pathlib import Path

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM))

from chat import orchestrator as chat_orchestrator
from inference import client as brain_runtime_client
from inference import config as inference_config
from integrations import challenges as integration_challenges
from local.chat import continuation as local_chat_continuations
from local.chat import continuation_store as local_chat_continuation_store
from power import challenges as power_challenges
from power import human as power_human

IMAGE = "registry.example/assistant@sha256:" + "b" * 64
TURN = brain_runtime_client.RuntimeTurn(
    status="power-required",
    reply="",
    powers=(
        brain_runtime_client.PowerRequest(
            interrupt_id="power-1",
            assistant_id="demo-assistant",
            power="publish",
            input={"message": "private Power input"},
        ),
    ),
)


def pending() -> local_chat_continuations.PendingLocalChat:
    return local_chat_continuations.PendingLocalChat(
        continuation=chat_orchestrator.ChatContinuation(
            turn=TURN,
            seen_interrupts=("older-power",),
            invoked=(chat_orchestrator.InvokedPower("demo-assistant", "lookup"),),
            round_index=1,
        ),
        assistant_ids=("demo-assistant",),
        file_ids=("a" * 32,),
        provider="openai",
        identity=(
            "Demo Team",
            "network-id",
            (("demo-assistant", IMAGE, "container-id"),),
            [
                {
                    "id": "a" * 32,
                    "name": "brief.txt",
                    "media_type": "text/plain",
                    "size": 42,
                }
            ],
            inference_config.normalize("openai", "gpt-5.5"),
        ),
    )


class LocalChatContinuationCodecTests(unittest.TestCase):
    def _round_trip(self, kind: str, requirements: tuple[object, ...]) -> None:
        bindings, payload = local_chat_continuations.encode(kind, requirements, pending())
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
        self.assertEqual(decoded.pending, pending())

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

    def test_round_trips_human_suspension_and_nonsecret_transcript(self) -> None:
        first = {
            "kind": "approval",
            "ordinal": 0,
            "title": "Prepare",
            "description": "Prepare the reviewed action.",
        }
        first["fingerprint"] = power_human._fingerprint(first)
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
        current["fingerprint"] = power_human._fingerprint(current)
        state = pending()
        state = local_chat_continuations.PendingLocalChat(
            state.continuation,
            state.assistant_ids,
            state.file_ids,
            state.provider,
            state.identity,
            (
                power_human.PowerTranscript(
                    "power-1",
                    (power_human.admit_response(power_human.validate_request(first, ("approval",)), True),),
                ),
            ),
        )
        requirement = (
            power_challenges.HumanRequirement(
                "demo-assistant",
                "Demo Assistant",
                "publish",
                "Publish a DNS record.",
                "power-1",
                power_human.validate_request(current, ("input:text",)),
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
        secret_request["fingerprint"] = power_human._fingerprint(secret_request)
        state = pending()
        state = local_chat_continuations.PendingLocalChat(
            state.continuation,
            state.assistant_ids,
            state.file_ids,
            state.provider,
            state.identity,
            (
                power_human.PowerTranscript(
                    "power-1",
                    (
                        power_human.admit_response(
                            power_human.validate_request(secret_request, ("input:password",)), "secret"
                        ),
                    ),
                ),
            ),
        )

        with self.assertRaisesRegex(local_chat_continuations.ContinuationCodecError, "secret"):
            local_chat_continuations.encode(
                "human",
                (
                    power_challenges.HumanRequirement(
                        "demo-assistant",
                        "Demo Assistant",
                        "publish",
                        "Publish a DNS record.",
                        "power-1",
                        power_human.validate_request(secret_request, ("input:password",)),
                    ),
                ),
                state,
            )

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
