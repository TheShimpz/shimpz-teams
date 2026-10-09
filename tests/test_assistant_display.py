"""Team admission of an Assistant's displayed static copy: description, Creator links, and Action descriptions."""

import unittest

from assistant import manifest as assistant_manifest
from protocol.http.v1 import payload as http_payload
from tests import catalog_fixtures
from tests.test_assistant_manifest import manifest

LABEL = "API key"
STORED_INPUT = assistant_manifest.canonical_stored_input_declarations(
    {"api-key": {"kind": "password", "label": LABEL, "description": "The provider key."}}
)


def _action(description: object = catalog_fixtures.ACTION_DESCRIPTION, **members: object) -> dict[str, object]:
    action = {
        "id": "run",
        "description": description,
        "input_schema": {"type": "object", "additionalProperties": False},
        "output_schema": {"type": "object", "additionalProperties": False},
        "integrations": [],
        "stored_inputs": [],
        "input_files": [],
        "human_requests": [],
        "effect": "read_only",
        **members,
    }
    if description is None:
        del action["description"]
    return action


def _admit(
    action: dict[str, object], *extra: dict[str, object], description: str | None = None, labeled: bool = True
) -> dict[str, object]:
    """Admit one Action under the fixture catalog, the declared label's message unless unlabeled, and ``extra``."""
    label = (catalog_fixtures.message(LABEL, 120),) if labeled else ()
    messages = catalog_fixtures.messages(catalog_fixtures.SUMMARY, *label, *extra)
    return assistant_manifest.canonical_machine_contract(
        {"version": 1, "actions": [action], "messages": messages},
        (),
        STORED_INPUT,
        summary=catalog_fixtures.SUMMARY,
        description=catalog_fixtures.ASSISTANT_DESCRIPTION if description is None else description,
        allowed_hosts=(),
    )


class ManifestPresentationTests(unittest.TestCase):
    def test_the_description_and_links_are_exposed_in_canonical_display_order(self) -> None:
        raw = manifest().replace(
            b"\n[network]",
            b'\n[shimpz.links]\ngithub = "https://github.com/TheShimpz"\nsite = "https://shimpz.com/"\n\n[network]',
        )
        presentation = assistant_manifest.parse_manifest_presentation(raw)
        self.assertEqual(presentation.description, catalog_fixtures.ASSISTANT_DESCRIPTION)
        self.assertEqual(dict(presentation.links), catalog_fixtures.LINKS)
        self.assertEqual(list(presentation.links), ["site", "github"])
        self.assertEqual(assistant_manifest.parse_manifest_presentation(manifest()).links, {})

    def test_a_description_must_be_one_printable_normalized_public_line(self) -> None:
        for description in ("Uses a\\u00a0no-break space.", "Cafe\\u0301 notes.", "api_key = abcdefghijklmnop"):
            with self.subTest(description=description), self.assertRaises(assistant_manifest.ManifestError):
                assistant_manifest.parse_manifest_contract(manifest(description=description))

    def test_record_and_resolution_links_may_be_empty_but_never_invalid(self) -> None:
        empty = assistant_manifest.canonical_manifest_presentation(
            description=catalog_fixtures.ASSISTANT_DESCRIPTION, links={}
        )
        self.assertEqual(dict(empty.links), {})
        for links in (None, {"mastodon": "https://mastodon.social/"}, {"x": "https://twitter.com/shimpz"}):
            with self.subTest(links=links), self.assertRaisesRegex(assistant_manifest.ManifestError, "links"):
                assistant_manifest.canonical_manifest_presentation(
                    description=catalog_fixtures.ASSISTANT_DESCRIPTION, links=links
                )

    def test_creator_links_follow_each_kinds_host_and_the_help_url_grammar(self) -> None:
        self.assertEqual(
            http_payload.canonical_creator_links(
                {"instagram": "https://www.instagram.com/shimpz", "youtube": "https://youtube.com/@shimpz"}
            ),
            {"youtube": "https://youtube.com/@shimpz", "instagram": "https://www.instagram.com/shimpz"},
        )
        for links in (
            [],
            {"site": 1},
            {"site": "https://shimpz.com/" + "a" * 238},
            {"linkedin": "https://linkedin.example/in/shimpz"},
            {"site": "http://shimpz.com/"},
        ):
            with self.subTest(links=links):
                self.assertIsNone(http_payload.canonical_creator_links(links))
        self.assertIsNotNone(http_payload.canonical_creator_links({"site": "https://shimpz.com/" + "a" * 237}))


class ActionDescriptionTests(unittest.TestCase):
    def test_every_action_declares_one_printable_line_of_at_most_eighty_characters(self) -> None:
        self.assertEqual(_admit(_action())["actions"][0]["description"], catalog_fixtures.ACTION_DESCRIPTION)
        for description in (None, "", " Leading.", "x" * 81, "Tab\tinside.", "Café.", 3):
            with self.subTest(description=description), self.assertRaises(assistant_manifest.ManifestError):
                _admit(_action(description))


class DisplayCatalogTests(unittest.TestCase):
    def test_each_displayed_text_is_one_parameterless_message_within_its_bound(self) -> None:
        line = catalog_fixtures.message("Read one value.", 120)
        self.assertEqual(len(_admit(_action("Read one value."), line)["actions"]), 1)
        for refused in (
            # The Action description is not cataloged, cataloged past its line bound, or cataloged with a parameter.
            (_action("Read one value."),),
            (_action("Read one value."), catalog_fixtures.message("Read one value.", 160)),
            (
                _action("Read {record}."),
                catalog_fixtures.message("Read {record}.", 120, catalog_fixtures.ZONE_PARAMS[:1]),
            ),
        ):
            with (
                self.subTest(refused=refused[0]["description"]),
                self.assertRaisesRegex(assistant_manifest.ManifestError, "displayed copy"),
            ):
                _admit(*refused)
        # Every declared Stored Input label is displayed, so each one is cataloged like any other displayed line.
        labeled = _action(stored_inputs=["api-key"], human_requests=["input:password"])
        self.assertEqual(len(_admit(labeled)["actions"]), 1)
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "displayed copy"):
            _admit(labeled, labeled=False)

    def test_the_assistant_description_is_cataloged_within_its_paragraph_bound(self) -> None:
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "displayed copy"):
            _admit(_action(), description="An uncataloged description.")

    def test_one_message_serves_every_use_of_its_text_at_the_tightest_bound(self) -> None:
        shared = "Read and describe one value."
        # The same text as the Assistant description and an Action description: its one message must fit the line.
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "displayed copy"):
            _admit(_action(shared), catalog_fixtures.message(shared, 500), description=shared)
        admitted = _admit(_action(shared), catalog_fixtures.message(shared, 120), description=shared)
        self.assertEqual(admitted["actions"][0]["description"], shared)


if __name__ == "__main__":
    unittest.main()
