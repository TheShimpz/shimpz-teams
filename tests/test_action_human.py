import json
import unittest

from action import human
from protocol.assistant.v1.validators import message_catalog as catalog_validator
from tests import catalog_fixtures, human_request_fixtures

CATALOG = human_request_fixtures.CATALOG


def request(kind: str, ordinal: int = 0, **fields: object) -> human.HumanRequest:
    return human_request_fixtures.request(kind, ordinal, **fields)


class HumanResponseTests(unittest.TestCase):
    def test_approval_and_auth_require_success(self) -> None:
        for kind in human.AUTHORIZATION_KINDS:
            with self.subTest(kind=kind):
                current = request(kind)
                self.assertTrue(human.admit_response(current, True).value)
                with self.assertRaises(human.HumanRequestError):
                    human.admit_response(current, False)

    def test_text_response_obeys_reviewed_bounds(self) -> None:
        current = request(
            "input:text",
            label="Zone",
            required=True,
            placeholder=None,
            min_length=3,
            max_length=8,
        )

        self.assertEqual(human.admit_response(current, "shimpz").value, "shimpz")
        for invalid in ("", "ab", "too-long-value", None):
            with self.subTest(value=invalid), self.assertRaises(human.HumanRequestError):
                human.admit_response(current, invalid)

    def test_single_and_multiple_choices_are_closed_to_reviewed_values(self) -> None:
        options = [
            {"value": "safe", "label": "Safe", "description": None},
            {"value": "fast", "label": "Fast", "description": "Use the faster path."},
        ]
        single = request("input:choice", label="Mode", required=True, options=options)
        multiple = request(
            "input:choices",
            label="Modes",
            required=True,
            options=options,
            min_selections=1,
            max_selections=2,
        )

        self.assertEqual(human.admit_response(single, "safe").value, "safe")
        self.assertEqual(human.admit_response(multiple, ["safe", "fast"]).value, ["safe", "fast"])
        for current, invalid in ((single, "other"), (multiple, []), (multiple, ["safe", "safe"])):
            with self.subTest(value=invalid), self.assertRaises(human.HumanRequestError):
                human.admit_response(current, invalid)

    def test_transcript_requires_exact_sequence_and_a_password_is_only_a_stored_input(self) -> None:
        approval = request("approval")
        text = request(
            "input:text", 1, label="Provider id", required=True, placeholder=None, min_length=1, max_length=64
        )
        transcript = human.ActionTranscript("interrupt-1").append(approval, True).append(text, "act_1")

        self.assertEqual([item["ordinal"] for item in transcript.payloads()], [0, 1])
        with self.assertRaises(human.HumanRequestError):
            transcript.append(request("approval", 3), True)
        # A password request that names no Stored Input is refused: its value would reach the Action (ADR-0106).
        with self.assertRaises(human.HumanRequestError):
            request(
                "input:password",
                2,
                label="Provider secret",
                required=True,
                placeholder=None,
                min_length=1,
                max_length=64,
            )
        with self.assertRaisesRegex(human.HumanRequestError, "authorization more than once"):
            human.ActionTranscript("interrupt-1").append(approval, True).append(request("auth:password", 1), True)
        with self.assertRaises(human.HumanRequestError):
            human.ActionTranscript("interrupt-1").append(request("approval", 1), True)

    def test_stored_input_password_is_exactly_declared_and_kept_out_of_replay(self) -> None:
        descriptor = human_request_fixtures.fingerprinted(
            {
                "kind": "input:password",
                "ordinal": 0,
                "title": "Connect WhatsApp",
                "description": "Provide the token once to continue this Action.",
                "label": "WhatsApp token",
                "required": True,
                "placeholder": None,
                "min_length": 1,
                "max_length": 1024,
                "stored_input": "whatsapp-token",
            }
        )

        current = human.validate_request(descriptor, ("input:password",), ("whatsapp-token",), catalog=CATALOG)
        self.assertEqual(current.stored_input, "whatsapp-token")
        # A Stored Input answer never enters the replay transcript; the admission carries it for Team to seal.
        with self.assertRaisesRegex(human.HumanRequestError, "answered by injection"):
            human.ActionTranscript("interrupt-1").append(current, "private-token")
        admission = human.append_response((), "interrupt-1", current, "private-token", 3)
        self.assertEqual(admission.transcripts, ())
        self.assertEqual(admission.requests_used, 4)
        self.assertEqual(
            (admission.stored_input.stored_input, admission.stored_input.value), ("whatsapp-token", "private-token")
        )
        self.assertNotIn("private-token", repr(admission))
        for refused_value in ("", 7, "x" * 1025):
            with self.subTest(value=refused_value), self.assertRaises(human.HumanRequestError):
                human.append_response((), "interrupt-1", current, refused_value, 0)
        with self.assertRaisesRegex(human.HumanRequestError, "human request limit"):
            human.append_response((), "interrupt-1", current, "private-token", human.MAX_REQUESTS_PER_TURN)
        for stored_inputs in ((), ("other-token",)):
            with (
                self.subTest(stored_inputs=stored_inputs),
                self.assertRaisesRegex(
                    human.HumanRequestError,
                    "undeclared",
                ),
            ):
                human.validate_request(descriptor, ("input:password",), stored_inputs, catalog=CATALOG)

        malformed = human_request_fixtures.fingerprinted({**descriptor, "stored_input": "WhatsApp_Token"})
        with self.assertRaises(human.HumanRequestError):
            human.validate_request(malformed, ("input:password",), ("whatsapp-token",), catalog=CATALOG)

    def test_stored_input_answer_keeps_the_ordinal_rule(self) -> None:
        def stored(ordinal: int, stored_input: str) -> human.HumanRequest:
            return human.validate_request(
                human_request_fixtures.fingerprinted(
                    {
                        "kind": "input:password",
                        "ordinal": ordinal,
                        "title": "Connect WhatsApp",
                        "description": "Provide the token once to continue this Action.",
                        "label": "WhatsApp token",
                        "required": True,
                        "placeholder": None,
                        "min_length": 1,
                        "max_length": 1024,
                        "stored_input": stored_input,
                    }
                ),
                ("input:password", "approval"),
                ("app-secret", "whatsapp-token"),
                catalog=CATALOG,
            )

        approved = human.ActionTranscript("interrupt-1").append(request("approval"), True)
        # Two slots answered one after the other reuse the same next ordinal, since neither enters the transcript.
        first = human.append_response((approved,), "interrupt-1", stored(1, "whatsapp-token"), "token", 1)
        second = human.append_response(first.transcripts, "interrupt-1", stored(1, "app-secret"), "secret", 2)
        self.assertEqual(second.transcripts, (approved,))
        self.assertEqual(second.requests_used, 3)
        self.assertEqual(second.stored_input.stored_input, "app-secret")
        with self.assertRaisesRegex(human.HumanRequestError, "sequence is invalid"):
            human.append_response((approved,), "interrupt-1", stored(2, "app-secret"), "secret", 2)

    def test_turn_transcripts_are_interrupt_bound_and_globally_bounded(self) -> None:
        transcripts: tuple[human.ActionTranscript, ...] = ()
        for index in range(human.MAX_REQUESTS_PER_TURN):
            interrupt = f"interrupt-{index // human.MAX_REQUESTS_PER_ACTION}"
            ordinal = index % human.MAX_REQUESTS_PER_ACTION
            current = request(
                "input:text",
                ordinal,
                label="Value",
                required=True,
                placeholder=None,
                min_length=1,
                max_length=8,
            )
            admission = human.append_response(
                transcripts,
                interrupt,
                current,
                "value",
                index,
            )
            transcripts = admission.transcripts

        self.assertEqual(len(transcripts), 2)
        self.assertEqual(len(human.transcript_for(transcripts, "interrupt-1").responses), 8)
        with self.assertRaises(human.HumanRequestError):
            human.append_response(
                transcripts,
                "interrupt-2",
                request(
                    "input:text",
                    label="Value",
                    required=True,
                    placeholder=None,
                    min_length=1,
                    max_length=8,
                ),
                "value",
                human.MAX_REQUESTS_PER_TURN,
            )
        with self.assertRaises(human.HumanRequestError):
            human.transcript_for((*transcripts, transcripts[0]), "interrupt-0")

        self.assertEqual(admission.requests_used, human.MAX_REQUESTS_PER_TURN)
        self.assertEqual(
            human.retain_unfinished_transcripts(transcripts, ("interrupt-0",)),
            (transcripts[1],),
        )

    def test_request_admission_rejects_shape_capability_and_fingerprint_drift(self) -> None:
        with self.assertRaises(human.HumanRequestError):
            human.validate_request({}, ("approval",), catalog=CATALOG)
        descriptor = {**human_request_fixtures.descriptor("approval"), "fingerprint": "0" * 64}
        with self.assertRaisesRegex(human.HumanRequestError, "fingerprint"):
            human.validate_request(descriptor, ("approval",), catalog=CATALOG)
        # A fingerprint that is not exactly 64 lowercase ASCII hex characters fails closed before any comparison.
        canonical = human._fingerprint({key: value for key, value in descriptor.items() if key != "fingerprint"})
        malformed_fingerprints = (
            "\u00e9" * 64,
            canonical[:-1] + "\u00e9",
            "\uff10" * 64,
            "A" * 64,
            canonical.upper(),
            "0" * 63,
            "0" * 64 + "\n",
            canonical + "\n",
        )
        for malformed_fingerprint in malformed_fingerprints:
            malformed_request = {**descriptor, "fingerprint": malformed_fingerprint}
            with (
                self.subTest(fingerprint=malformed_fingerprint),
                self.assertRaisesRegex(human.HumanRequestError, "^Assistant Action human request is invalid$"),
            ):
                human.validate_request(malformed_request, ("approval",), catalog=CATALOG)
        admitted = human.validate_request({**descriptor, "fingerprint": canonical}, ("approval",), catalog=CATALOG)
        self.assertEqual(admitted.fingerprint, canonical)
        with self.assertRaises(human.HumanRequestError):
            human.validate_request(human_request_fixtures.descriptor("approval"), (), catalog=CATALOG)
        with self.assertRaises(human.HumanRequestError):
            human._canonical({"value": object()})

        malformed = human.HumanRequest("approval", 0, "0" * 64, b"[]")
        with self.assertRaises(AssertionError):
            malformed.payload()

    def test_copy_must_reference_declared_messages_with_exactly_their_parameters(self) -> None:
        zone = catalog_fixtures.ref(catalog_fixtures.ZONE_TITLE, record="rec-1", zone="example.com")
        admitted = human_request_fixtures.admit(human_request_fixtures.descriptor("approval", title=zone))
        self.assertEqual(admitted.payload()["title"], zone)
        self.assertEqual(
            [message["msgid"] for message in admitted.messages()],
            sorted(
                (catalog_fixtures.ZONE_TITLE, human_request_fixtures.DESCRIPTION),
                key=catalog_validator.message_id,
            ),
        )

        refused = {
            "plain string copy": {"title": "Continue safely"},
            "undeclared message": {"title": {"message": "0" * 64, "params": {}}},
            "missing parameter": {"title": catalog_fixtures.ref(catalog_fixtures.ZONE_TITLE, record="rec-1")},
            "extra parameter": {"title": catalog_fixtures.ref(catalog_fixtures.TITLE, zone="example.com")},
            "parameter of the wrong kind": {
                "title": catalog_fixtures.ref(catalog_fixtures.ZONE_TITLE, record="rec-1", zone="not a domain")
            },
            "message bound wider than its field": {"title": catalog_fixtures.ref(catalog_fixtures.DESCRIPTION)},
        }
        for name, fields in refused.items():
            descriptor = {
                "kind": "approval",
                "ordinal": 0,
                "title": catalog_fixtures.ref(catalog_fixtures.TITLE),
                "description": catalog_fixtures.ref(catalog_fixtures.DESCRIPTION),
                **fields,
            }
            descriptor["fingerprint"] = human._fingerprint(descriptor)
            with self.subTest(name), self.assertRaises(human.HumanRequestError):
                human.validate_request(descriptor, ("approval",), catalog=CATALOG)

    def test_the_fingerprint_covers_references_and_never_a_display_language(self) -> None:
        first = human_request_fixtures.descriptor("approval")
        changed = human_request_fixtures.descriptor("approval", title="Approve this other Action")
        self.assertNotEqual(first["fingerprint"], changed["fingerprint"])
        self.assertEqual(
            first["fingerprint"],
            human._fingerprint({key: value for key, value in first.items() if key != "fingerprint"}),
        )

    def test_response_helpers_reject_malformed_replay_descriptors(self) -> None:
        single = human.HumanRequest(
            "input:choice",
            0,
            "0" * 64,
            json.dumps({"options": None}).encode(),
        )
        with self.assertRaises(human.HumanRequestError):
            human.admit_response(single, "value")


if __name__ == "__main__":
    unittest.main()
