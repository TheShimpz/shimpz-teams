"""The reviewed Stored Input key page and the Brain's purpose on a Team human request (ADR-0090)."""

import copy
import time
import unittest
from dataclasses import replace

import test_local_chat_continuations

from action import challenges as action_challenges
from action import human as action_human
from assistant import manifest as assistant_manifest
from assistant import spec as assistant_registry
from local.chat import continuation
from protocol.assistant.v1.validators import message_catalog as catalog_validator
from protocol.http.v1 import payload as http_payload
from tests import catalog_fixtures, human_request_fixtures

HELP_URL = "https://dashboard.exa.ai/api-keys"
PURPOSE = "To bring today's AI news, I need to search the web with Exa."


def _request(kind: str, *, stored_input: str | None = None) -> action_human.HumanRequest:
    fields: dict[str, object] = {"title": "Exa API key", "description": "Key."}
    if kind == "input:password":
        fields.update(label="Exa API key", required=True, placeholder=None, min_length=1, max_length=128)
        if stored_input is not None:
            fields["stored_input"] = stored_input
    stored = (stored_input,) if stored_input is not None else ()
    return human_request_fixtures.request(kind, stored_inputs=stored, **fields)


def _requirement(request: action_human.HumanRequest, **presentation) -> action_challenges.HumanRequirement:
    if presentation.get("purpose") is not None:
        presentation.setdefault("purpose_locale", "en")
    return human_request_fixtures.requirement(
        request,
        assistant_id="shimpz-exa",
        assistant_name="Exa",
        action_id="search-web",
        action_summary="Search the web.",
        assistant_version="0.1.2",
        **presentation,
    )


class DeclarationTests(unittest.TestCase):
    def test_a_declaration_carries_its_key_page_only_when_declared(self) -> None:
        declared = assistant_manifest.StoredInputDeclaration("exa-api-key", "password", "Key", "Exa key.", HELP_URL)
        plain = assistant_manifest.StoredInputDeclaration("exa-api-key", "password", "Key", "Exa key.")
        self.assertEqual(
            declared.document(),
            {"id": "exa-api-key", "kind": "password", "label": "Key", "description": "Exa key.", "help_url": HELP_URL},
        )
        self.assertNotIn("help_url", plain.metadata())
        documents = [declared.document()]
        self.assertEqual(assistant_manifest.stored_input_declarations_from_documents(documents), (declared,))
        spec = assistant_registry.StoredInputSpec(**declared.metadata())
        contract = assistant_manifest.reviewed_manifest_contract(
            allowed_hosts=[], integrations={}, stored_inputs={"exa-api-key": spec}
        )
        self.assertEqual(contract.stored_inputs, (declared,))

    def test_an_invalid_or_unknown_declaration_field_fails_closed(self) -> None:
        base = {"kind": "password", "label": "Key", "description": "Exa key."}
        for metadata in (
            {**base, "help_url": "http://dashboard.exa.ai/api-keys"},
            {**base, "help_url": "https://dashboard.exa.ai"},
            {**base, "help_url": None},
            {**base, "url": HELP_URL},
        ):
            with self.subTest(metadata=metadata), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.canonical_stored_input_declarations({"exa-api-key": metadata})


class ChallengeTests(unittest.TestCase):
    def test_only_the_named_reviewed_stored_input_supplies_a_key_page(self) -> None:
        declarations = {"exa-api-key": assistant_registry.StoredInputSpec("password", "Key", "Exa key.", HELP_URL)}
        stored = _request("input:password", stored_input="exa-api-key")
        self.assertEqual(action_challenges.declared_help_url(stored, declarations), HELP_URL)
        self.assertIsNone(action_challenges.declared_help_url(_request("approval"), declarations))
        self.assertIsNone(action_challenges.declared_help_url(_request("input:password"), declarations))
        self.assertIsNone(action_challenges.declared_help_url(stored, {}))
        self.assertIsNone(action_challenges.declared_help_url(stored, None))
        undeclared = {"exa-api-key": assistant_registry.StoredInputSpec("password", "Key", "Exa key.")}
        self.assertIsNone(action_challenges.declared_help_url(stored, undeclared))

    def test_the_projection_carries_presentation_beside_the_request_only_when_present(self) -> None:
        stored = _request("input:password", stored_input="exa-api-key")
        for presentation, expected in (
            ({}, {}),
            ({"purpose": PURPOSE}, {"purpose": PURPOSE}),
            ({"purpose": PURPOSE, "help_url": HELP_URL}, {"purpose": PURPOSE, "help_url": HELP_URL}),
        ):
            with self.subTest(presentation=presentation):
                challenge = action_challenges.PendingHumanChallenge(
                    "c" * 32, "team_1", time.monotonic() + 60, _requirement(stored, **presentation), None
                )
                payload = action_challenges.challenge_payload(challenge)
                self.assertEqual({key: payload[key] for key in ("purpose", "help_url") if key in payload}, expected)
                self.assertEqual(payload["request"], stored.payload())

    def test_a_requirement_with_misplaced_or_invalid_presentation_is_refused(self) -> None:
        stored = _request("input:password", stored_input="exa-api-key")
        for requirement in (
            _requirement(_request("approval"), help_url=HELP_URL),
            _requirement(_request("input:password"), help_url=HELP_URL),
            _requirement(stored, help_url="https://dashboard.exa.ai"),
            _requirement(stored, purpose="Search — then read"),
        ):
            with self.subTest(requirement=requirement):
                challenge = action_challenges.PendingHumanChallenge(
                    "c" * 32, "team_1", time.monotonic() + 60, requirement, None
                )
                with self.assertRaises(action_challenges.HumanChallengeError):
                    action_challenges.challenge_payload(challenge)


class ContinuationTests(unittest.TestCase):
    def test_a_paused_request_restores_its_presentation_exactly(self) -> None:
        stored = _request("input:password", stored_input="exa-api-key")
        for requirement in (
            _requirement(stored),
            _requirement(stored, purpose=PURPOSE, help_url=HELP_URL),
            _requirement(_request("approval"), purpose=PURPOSE),
        ):
            with self.subTest(requirement=requirement):
                [encoded] = continuation._requirements_payload("human", (requirement,))
                self.assertEqual(continuation._human_requirement(encoded), requirement)

    def test_a_record_with_invalid_presentation_fails_closed(self) -> None:
        stored = _request("input:password", stored_input="exa-api-key")
        for requirement in (
            replace(_requirement(_request("approval")), help_url=HELP_URL),
            _requirement(stored, help_url="https://dashboard.exa.ai"),
            _requirement(stored, purpose="Visit www.example.com"),
        ):
            with self.subTest(requirement=requirement):
                [encoded] = continuation._requirements_payload("human", (requirement,))
                with self.assertRaises(continuation.ContinuationCodecError):
                    continuation._human_requirement(encoded)


class LocalizedContinuationTests(unittest.TestCase):
    """A paused request keeps its referenced catalog entries, rendered copy, and purpose locale (ADR-0091)."""

    def _encoded(self, **presentation) -> dict[str, object]:
        request = human_request_fixtures.request(
            "input:choice",
            label="Mode",
            required=True,
            options=[
                {"value": "safe", "label": "Safe", "description": None},
                {"value": "fast", "label": "Fast", "description": "Use the faster path."},
            ],
        )
        requirement = human_request_fixtures.requirement(request, locale="ja", **presentation)
        [encoded] = continuation._requirements_payload("human", (requirement,))
        self.assertEqual(continuation._human_requirement(copy.deepcopy(encoded)), requirement)
        return encoded

    def test_the_rendered_copy_and_purpose_locale_restore_exactly(self) -> None:
        encoded = self._encoded(purpose=PURPOSE, purpose_locale="ja")
        self.assertEqual(encoded["copy"]["locale"], "ja")
        self.assertEqual(encoded["purpose_locale"], "ja")
        self.assertEqual(
            [item["msgid"] for item in encoded["messages"]],
            sorted((item["msgid"] for item in encoded["messages"]), key=catalog_validator.message_id),
        )

    def test_a_record_whose_catalog_copy_or_purpose_locale_drifted_fails_closed(self) -> None:
        encoded = self._encoded(purpose=PURPOSE, purpose_locale="ja")
        undeclared = [item for item in encoded["messages"] if item["msgid"] != "Mode"]
        extra = [*encoded["messages"], catalog_fixtures.message("Unreferenced copy.")]
        extra.sort(key=lambda item: item["id"])
        mutations = {
            "messages are not a list": {"messages": {}},
            "a message whose id is not its template hash": {
                "messages": [{**encoded["messages"][0], "msgid": "Changed"}, *encoded["messages"][1:]]
            },
            "a referenced message is missing": {"messages": undeclared},
            "an unreferenced message is kept": {"messages": extra},
            "unknown copy locale": {"copy": {**encoded["copy"], "locale": "xx"}},
            "malformed pack digest": {"copy": {**encoded["copy"], "pack_digest": "sha256:short"}},
            "malformed catalog digest": {"copy": {**encoded["copy"], "catalog_digest": None}},
            "rendered copy of another request": {"copy": {**encoded["copy"], "rendered": {"title": "Other"}}},
            "copy with an extra field": {"copy": {**encoded["copy"], "extra": True}},
            "purpose without its locale": {"purpose_locale": None},
            "purpose locale without a purpose": {"purpose": None},
            "unknown purpose locale": {"purpose_locale": "xx"},
        }
        for name, changes in mutations.items():
            with self.subTest(name), self.assertRaises(continuation.ContinuationCodecError):
                continuation._human_requirement({**copy.deepcopy(encoded), **changes})

    def test_the_turn_locale_persists_with_the_pending_continuation(self) -> None:
        pending = test_local_chat_continuations.pending()
        for locale in (None, "ar"):
            with self.subTest(locale=locale):
                raw = continuation._pending_payload(replace(pending, locale=locale))
                self.assertEqual(continuation._pending(raw).locale, locale)
        with self.assertRaisesRegex(continuation.ContinuationCodecError, "locale"):
            continuation._pending({**continuation._pending_payload(pending), "locale": "pt-BR"})


class ProtocolTests(unittest.TestCase):
    def test_purpose_and_key_page_admission(self) -> None:
        self.assertEqual(http_payload.canonical_purpose(PURPOSE), PURPOSE)
        self.assertIsNone(http_payload.canonical_purpose("Search - then read"))
        self.assertEqual(http_payload.canonical_help_url(HELP_URL), HELP_URL)
        self.assertIsNone(http_payload.canonical_help_url(HELP_URL + "\n"))


if __name__ == "__main__":
    unittest.main()
