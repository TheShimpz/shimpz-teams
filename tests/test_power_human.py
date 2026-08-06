import unittest

from power import human


def request(kind: str, ordinal: int = 0, **fields: object) -> human.HumanRequest:
    descriptor = {
        "kind": kind,
        "ordinal": ordinal,
        "title": "Continue safely",
        "description": "Provide the reviewed value before the Power continues.",
        **fields,
    }
    descriptor["fingerprint"] = human._fingerprint(descriptor)
    return human.validate_request(descriptor, (kind,))


class HumanResponseTests(unittest.TestCase):
    def test_approval_and_auth_require_success(self) -> None:
        for kind in ("approval", "auth:reauth", "auth:second-factor", "auth:phishing-resistant"):
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

    def test_transcript_requires_exact_sequence_and_keeps_password_secret_last(self) -> None:
        approval = request("approval")
        password = request(
            "input:password",
            1,
            label="Provider secret",
            required=True,
            placeholder=None,
            min_length=1,
            max_length=64,
        )
        transcript = human.PowerTranscript("interrupt-1").append(approval, True).append(password, "secret")

        self.assertEqual([item["ordinal"] for item in transcript.payloads()], [0, 1])
        self.assertEqual(transcript.protected_values(), {"human-response-1": "secret"})
        with self.assertRaises(human.HumanRequestError):
            transcript.append(request("approval", 2), True)
        with self.assertRaises(human.HumanRequestError):
            human.PowerTranscript("interrupt-1").append(request("approval", 1), True)

    def test_turn_transcripts_are_interrupt_bound_and_globally_bounded(self) -> None:
        transcripts: tuple[human.PowerTranscript, ...] = ()
        for index in range(human.MAX_REQUESTS_PER_TURN):
            interrupt = f"interrupt-{index // human.MAX_REQUESTS_PER_POWER}"
            ordinal = index % human.MAX_REQUESTS_PER_POWER
            transcripts = human.append_response(
                transcripts,
                interrupt,
                request("approval", ordinal),
                True,
            )

        self.assertEqual(len(transcripts), 2)
        self.assertEqual(len(human.transcript_for(transcripts, "interrupt-1").responses), 8)
        with self.assertRaises(human.HumanRequestError):
            human.append_response(transcripts, "interrupt-2", request("approval"), True)
        with self.assertRaises(human.HumanRequestError):
            human.transcript_for((*transcripts, transcripts[0]), "interrupt-0")


if __name__ == "__main__":
    unittest.main()
