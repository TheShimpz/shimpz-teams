"""Human-request copy rendered per interface language from the binding's catalog and pack (ADR-0091)."""

import time
import unittest
from dataclasses import replace

from action import challenges as action_challenges
from assistant import language as assistant_language
from protocol.assistant.v1.validators import message_catalog as catalog_validator
from protocol.http.v1 import challenge as http_challenge
from tests import catalog_fixtures, human_request_fixtures

PURPOSE = "To clean up your zone, I need to delete one record."
ZONE = catalog_fixtures.ref(catalog_fixtures.ZONE_TITLE, record="rec-7", zone="example.com")


def _choice():
    return human_request_fixtures.request(
        "input:choices",
        title=ZONE,
        label="Records",
        required=True,
        options=[
            {"value": "a", "label": "First option", "description": None},
            {"value": "b", "label": "Second option", "description": "Choose this option to continue."},
        ],
        min_selections=1,
        max_selections=2,
    )


def _text():
    return human_request_fixtures.request(
        "input:text", label="Your answer", required=False, placeholder=None, min_length=0, max_length=8
    )


def _challenge(requirement: action_challenges.HumanRequirement) -> action_challenges.PendingHumanChallenge:
    return action_challenges.PendingHumanChallenge("c" * 32, "team_1", time.monotonic() + 60, requirement, None)


class RenderTests(unittest.TestCase):
    def test_every_copy_field_renders_in_each_interface_language_with_parameters_inserted_once(self) -> None:
        request = _choice()
        pack = human_request_fixtures.pack_for(request.messages())
        english = action_challenges.render_copy(request, pack, "en")

        self.assertEqual(
            english.rendered,
            {
                "title": "Delete the record rec-7 from example.com",
                "description": human_request_fixtures.DESCRIPTION,
                "label": "Records",
                "options": [
                    {"label": "First option", "description": None},
                    {"label": "Second option", "description": "Choose this option to continue."},
                ],
            },
        )
        self.assertEqual((english.catalog_digest, english.pack_digest), (pack.catalog_digest, pack.pack_digest))
        for locale in catalog_validator.LOCALES:
            with self.subTest(locale=locale):
                rendered = action_challenges.render_copy(request, pack, locale).rendered
                prefix = locale.upper()
                self.assertEqual(rendered["title"], f"{prefix} Delete the record rec-7 from example.com")
                self.assertEqual(rendered["options"][1]["label"], f"{prefix} Second option")
                self.assertIsNone(rendered["options"][0]["description"])
                self.assertEqual(http_challenge.canonical_rendered(rendered, request.payload()), rendered)
        # Option values, kinds, and the fingerprinted references stay canonical.
        self.assertEqual([option["value"] for option in request.payload()["options"]], ["a", "b"])

        text = _text()
        self.assertEqual(
            human_request_fixtures.copy(text, "ja").rendered,
            {
                "title": f"JA {human_request_fixtures.TITLE}",
                "description": f"JA {human_request_fixtures.DESCRIPTION}",
                "label": "JA Your answer",
                "placeholder": None,
            },
        )

    def test_rendering_refuses_an_unknown_locale_a_foreign_pack_and_an_unrenderable_translation(self) -> None:
        request = _choice()
        pack = human_request_fixtures.pack_for(request.messages())
        for locale in ("pt-BR", "EN", None):
            with self.subTest(locale=locale), self.assertRaisesRegex(action_challenges.HumanChallengeError, "locale"):
                action_challenges.render_copy(request, pack, locale)

        foreign = human_request_fixtures.pack_for(_text().messages())
        with self.assertRaisesRegex(action_challenges.HumanChallengeError, "binding"):
            action_challenges.render_copy(request, foreign, "en")
        redeclared = [
            {**message, "max_length": 500} if message["msgid"] == "Records" else message
            for message in request.messages()
        ]
        with self.assertRaisesRegex(action_challenges.HumanChallengeError, "binding"):
            action_challenges.render_copy(request, human_request_fixtures.pack_for(redeclared), "en")

        # A request that does not carry exactly the catalog entries it references is never rendered.
        detached = replace(request, catalog=b"[]")
        with self.assertRaisesRegex(action_challenges.HumanChallengeError, "binding"):
            action_challenges.render_copy(detached, pack, "en")

        label = catalog_validator.message_id("Records")
        broken = assistant_language.LanguagePack(
            pack.catalog_digest,
            pack.pack_digest,
            pack.messages,
            {**pack.translations, "de": {**pack.translations["de"], label: " untrimmed"}},
        )
        with self.assertRaisesRegex(action_challenges.HumanChallengeError, "rendered"):
            action_challenges.render_copy(request, broken, "de")


class ChallengeBindingTests(unittest.TestCase):
    def test_the_projection_binds_locale_pack_and_rendered_copy_beside_the_canonical_request(self) -> None:
        request = _choice()
        requirement = human_request_fixtures.requirement(request, locale="pt")
        payload = action_challenges.challenge_payload(_challenge(requirement))

        self.assertEqual(payload["request"], request.payload())
        self.assertEqual(payload["request"]["fingerprint"], request.fingerprint)
        self.assertEqual(payload["locale"], "pt")
        self.assertEqual(payload["pack_digest"], requirement.copy.pack_digest)
        self.assertEqual(payload["rendered"], requirement.copy.rendered)
        self.assertNotIn("purpose", payload)

    def test_a_purpose_is_projected_only_in_a_challenge_of_its_own_locale(self) -> None:
        request = _choice()
        same = human_request_fixtures.requirement(request, locale="pt", purpose=PURPOSE, purpose_locale="pt")
        other = human_request_fixtures.requirement(request, locale="de", purpose=PURPOSE, purpose_locale="pt")

        self.assertEqual(action_challenges.challenge_payload(_challenge(same))["purpose"], PURPOSE)
        self.assertNotIn("purpose", action_challenges.challenge_payload(_challenge(other)))

    def test_a_requirement_whose_copy_or_purpose_binding_is_malformed_is_refused(self) -> None:
        request = _choice()
        valid = human_request_fixtures.requirement(request, locale="fr")
        copy = valid.copy
        invalid = (
            replace(valid, copy=None),
            replace(valid, copy=replace(copy, locale="xx")),
            replace(valid, copy=replace(copy, locale=None)),
            replace(valid, copy=replace(copy, pack_digest=None)),
            replace(valid, copy=replace(copy, catalog_digest="sha256:short")),
            replace(valid, copy=replace(copy, pack_digest="0" * 64)),
            replace(valid, copy=replace(copy, rendered={**copy.rendered, "title": ""})),
            replace(valid, copy=replace(copy, rendered={**copy.rendered, "extra": "x"})),
            replace(valid, purpose=PURPOSE),
            replace(valid, purpose_locale="fr"),
            replace(valid, purpose=PURPOSE, purpose_locale="xx"),
        )
        for requirement in invalid:
            with self.subTest(requirement=requirement), self.assertRaises(action_challenges.HumanChallengeError):
                action_challenges.challenge_payload(_challenge(requirement))

    def test_relocalizing_needs_the_same_catalog_and_pack(self) -> None:
        request = _choice()
        pack = human_request_fixtures.pack_for(request.messages())
        english = human_request_fixtures.requirement(request, purpose=PURPOSE, purpose_locale="en")

        arabic = action_challenges.relocalize(english, pack, "ar")
        self.assertEqual(arabic.copy, action_challenges.render_copy(request, pack, "ar"))
        self.assertEqual((arabic.request, arabic.purpose), (english.request, english.purpose))
        self.assertNotIn("purpose", action_challenges.challenge_payload(_challenge(arabic)))

        other_policy = human_request_fixtures.pack_for(request.messages())
        repacked = assistant_language.admit_pack(
            catalog_fixtures.pack_bytes(request.messages(), f"sha256:{'8' * 64}"),
            request.messages(),
            catalog_fixtures.pack_digest(request.messages(), f"sha256:{'8' * 64}"),
        )
        self.assertEqual(other_policy.pack_digest, pack.pack_digest)
        with self.assertRaisesRegex(action_challenges.HumanChallengeError, "binding changed"):
            action_challenges.relocalize(english, repacked, "ar")

    def test_the_copy_binding_is_current_only_for_the_same_catalog_and_pack(self) -> None:
        request = _choice()
        requirement = human_request_fixtures.requirement(request)
        contract = {"messages": request.messages()}
        current = requirement.copy.pack_digest

        self.assertTrue(action_challenges.copy_binding_current(requirement, contract, current))
        self.assertFalse(action_challenges.copy_binding_current(requirement, contract, f"sha256:{'9' * 64}"))
        self.assertFalse(action_challenges.copy_binding_current(requirement, {"messages": _text().messages()}, current))


if __name__ == "__main__":
    unittest.main()
