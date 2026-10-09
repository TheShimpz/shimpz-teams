"""An installed Assistant's page in one interface language, on both profiles, from its exact current binding."""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from contextlib import nullcontext
from dataclasses import replace
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness

from assistant import details as assistant_details
from assistant import language as assistant_language
from assistant import manifest as assistant_manifest
from install import bindings
from install.contract import CONTRACT_ROOT
from local.assistant import api as assistant_api
from local.chat import types as chat_types
from local.errors import ApiProblemError
from local.install import snapshots
from local.install.registry import AssistantRegistry
from tests import catalog_fixtures
from tests.local_snapshot_fixtures import IMAGE_ID
from tests.local_snapshot_fixtures import client as snapshot_client

RESOLUTION = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())["fixtures"]["resolve_response"]["value"]
# Team's runtime contract closes every Action schema the open protocol fixture leaves open.
for _schema in ("input_schema", "output_schema"):
    RESOLUTION["machine_contract"]["actions"][0][_schema]["additionalProperties"] = False
MESSAGES = RESOLUTION["machine_contract"]["messages"]
PACK = assistant_language.admit_pack(
    catalog_fixtures.pack_bytes(MESSAGES), MESSAGES, catalog_fixtures.pack_digest(MESSAGES)
)
LEASE = object()
# The resolve fixture's page in Portuguese: each displayed text is its message's fixture translation.
PUBLISHED_PT = {
    "locale": "pt",
    "assistant_id": "hello-world",
    "assistant_version": "0.1.0",
    "name": "Hello World",
    "creators": ["@creator"],
    "summary": "PT Complete one reviewed greeting Action.",
    "description": "PT Greets one reviewed zone after your approval and reports exactly what it sent.",
    "links": {"site": "https://hello.example.org/", "github": "https://github.com/creator"},
    "actions": [{"id": "hello", "effect": "mutating", "description": "PT Greet one reviewed zone."}],
    "integrations": [{"id": "cloudflare", "provider": "cloudflare"}],
    "stored_inputs": [
        {"id": "whatsapp-app-secret", "label": "PT WhatsApp app secret"},
        {"id": "whatsapp-token", "label": "PT WhatsApp token"},
    ],
}


def _page(**changes: object) -> assistant_details.AssistantPage:
    page = assistant_details.AssistantPage(
        assistant_id="hello-world",
        version="0.1.0",
        name="Hello World",
        creators=("@creator",),
        summary=RESOLUTION["summary"],
        description=RESOLUTION["description"],
        links=RESOLUTION["links"],
        machine_contract=RESOLUTION["machine_contract"],
        integrations={"cloudflare": "cloudflare"},
        labels={item["id"]: item["label"] for item in RESOLUTION["stored_inputs"]},
    )
    return replace(page, **changes)


class AssistantPageTests(unittest.TestCase):
    def test_english_is_the_catalog_copy_and_any_other_language_its_pack_translation(self) -> None:
        self.assertEqual(_page().localized("pt", PACK), PUBLISHED_PT)
        english = _page().localized("en", None)
        self.assertEqual(english["description"], RESOLUTION["description"])
        self.assertEqual(english["stored_inputs"][1], {"id": "whatsapp-token", "label": "WhatsApp token"})

    def test_a_projection_outside_the_protocol_or_without_its_pack_fails_closed(self) -> None:
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "language pack"):
            _page().localized("pt", None)
        for page in (_page(name="Hello World"), _page(creators=()), _page(version="1")):
            with self.subTest(page=page), self.assertRaisesRegex(assistant_manifest.ManifestError, "details"):
                page.localized("en", None)


def _local_controller(directory: str, pack=PACK):
    store = bindings.DynamicAssistantStore(
        Path(directory) / "bindings.json", local_record_validator=snapshots.validate_record
    )
    registry = AssistantRegistry(store)
    container = SimpleNamespace(id="container-id")
    lifecycle = SimpleNamespace(
        _assistant_container=mock.Mock(return_value=container),
        _assistant_language=mock.Mock(return_value=pack),
    )
    controller = SimpleNamespace(_lock=lambda _team_id: nullcontext(), registry=registry, assistant_lifecycle=lifecycle)
    return controller, registry, lifecycle


class LocalInstalledDetailsTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = directory.name

    def test_a_published_binding_answers_its_resolution_page_from_the_binding_pack(self) -> None:
        controller, registry, lifecycle = _local_controller(self.directory)
        spec = registry.put("team_1", copy.deepcopy(RESOLUTION))

        self.assertEqual(assistant_api.assistant_details(controller, "team_1", "hello-world", "pt"), PUBLISHED_PT)
        lifecycle._assistant_container.assert_called_once_with("team_1", "hello-world")
        lifecycle._assistant_language.assert_called_once_with(
            chat_types.ActiveAssistant(spec, "container-id", lifecycle._assistant_container.return_value)
        )
        # English is the binding's own catalog copy and never reads the pack or the runtime.
        lifecycle._assistant_container.reset_mock()
        english = assistant_api.assistant_details(controller, "team_1", "hello-world", "en")
        self.assertEqual(english["summary"], RESOLUTION["summary"])
        lifecycle._assistant_container.assert_not_called()

    def test_a_local_binding_shows_its_snapshots_declared_creators(self) -> None:
        client, _image, _container = snapshot_client()
        record = snapshots.admit(client, IMAGE_ID).record
        messages = record["machine_contract"]["messages"]
        pack = assistant_language.admit_pack(
            catalog_fixtures.pack_bytes(messages), messages, catalog_fixtures.pack_digest(messages)
        )
        controller, registry, _lifecycle = _local_controller(self.directory, pack)
        registry.put_local("team_1", record)

        details = assistant_api.assistant_details(controller, "team_1", "fixture-assistant", "ja")
        self.assertEqual(details["creators"], ["@fixture"])
        self.assertEqual(details["description"], f"JA {catalog_fixtures.ASSISTANT_DESCRIPTION}")
        self.assertEqual(
            details["actions"],
            [{"id": "ping", "effect": "read_only", "description": "JA Run one reviewed test Action."}],
        )

    def test_absent_refused_and_inadmissible_bindings_and_bad_requests_fail_closed(self) -> None:
        controller, registry, lifecycle = _local_controller(self.directory)
        for locale, code in (("pt-BR", "invalid-locale"), ("pt", "assistant-not-installed")):
            with self.subTest(locale=locale), self.assertRaises(ApiProblemError) as caught:
                assistant_api.assistant_details(controller, "team_1", "hello-world", locale)
            self.assertEqual(caught.exception.code, code)

        registry.put("team_1", copy.deepcopy(RESOLUTION))
        refused = SimpleNamespace(admissible=False)
        with (
            mock.patch.object(registry, "binding", return_value=refused),
            self.assertRaises(ApiProblemError) as caught,
        ):
            assistant_api.assistant_details(controller, "team_1", "hello-world", "pt")
        self.assertEqual(caught.exception.code, "assistant-manifest-invalid")

        # A pack translation the protocol refuses is never sent.
        broken = SimpleNamespace(template=lambda _identifier, _locale: "Quebrado\nem duas linhas.")
        lifecycle._assistant_language.return_value = broken
        with self.assertRaises(ApiProblemError) as caught:
            assistant_api.assistant_details(controller, "team_1", "hello-world", "pt")
        self.assertEqual(caught.exception.code, "assistant-manifest-invalid")


lifecycle = harness.assistant_lifecycle
resources = harness.hosted_resources
state = harness.runtime_state


class HostedInstalledDetailsTests(unittest.TestCase):
    def setUp(self) -> None:
        binding = bindings.binding_from_resolution("team_1", copy.deepcopy(RESOLUTION))
        self.spec = lifecycle.publication.assistant_spec(binding)

    def test_the_binding_page_is_read_under_the_team_lock_from_its_pack(self) -> None:
        container = object()
        with (
            mock.patch.object(resources, "_require_current_authorization") as authorized,
            mock.patch.object(lifecycle, "_resolve_team_assistant", return_value=("hello-world", self.spec)) as resolve,
            mock.patch.object(resources, "_get_container", return_value=container) as get_container,
            mock.patch.object(lifecycle, "_assistant_language", return_value=PACK) as read_pack,
        ):
            self.assertEqual(lifecycle._assistant_details("team_1", "hello-world", "pt", LEASE), PUBLISHED_PT)
            authorized.assert_called_once_with("team_1", LEASE, require_isolation=False)
            resolve.assert_called_once_with("team_1", "hello-world")
            read_pack.assert_called_once_with(self.spec.contract, container)

            get_container.reset_mock()
            english = lifecycle._assistant_details("team_1", "hello-world", "en", LEASE)
            self.assertEqual(english["name"], "Hello World")
            get_container.assert_not_called()

            get_container.return_value = None
            with self.assertRaises(state.ApiError) as stopped:
                lifecycle._assistant_details("team_1", "hello-world", "ja", LEASE)
            self.assertEqual(stopped.exception.status, HTTPStatus.CONFLICT)

            get_container.return_value = container
            read_pack.return_value = SimpleNamespace(template=lambda _identifier, _locale: "")
            with self.assertRaises(state.ApiError) as invalid:
                lifecycle._assistant_details("team_1", "hello-world", "de", LEASE)
            self.assertEqual(invalid.exception.status, HTTPStatus.CONFLICT)

            resolve.side_effect = lifecycle.assistant_registry.AssistantSpecError("absent")
            with self.assertRaises(state.ApiError) as absent:
                lifecycle._assistant_details("team_1", "hello-world", "pt", LEASE)
            self.assertEqual(absent.exception.status, HTTPStatus.NOT_FOUND)

            with self.assertRaises(state.ApiError) as locale:
                lifecycle._assistant_details("team_1", "hello-world", "pt-BR", LEASE)
            self.assertEqual(locale.exception.status, HTTPStatus.UNPROCESSABLE_ENTITY)

    def test_the_route_answers_the_page_with_its_request_trace(self) -> None:
        request = harness.hosted_controller._AuthorizedRequest(
            {"assistant_id": "hello-world", "locale": "pt"}, "team_1", ("account", "account_1"), LEASE, {}
        )
        handler = object.__new__(harness.app.Handler)
        handler._send_json = mock.Mock()
        handler._audit_trace_id = "a" * 32
        with mock.patch.object(lifecycle, "_assistant_details", return_value=PUBLISHED_PT) as read:
            handler._route_assistant_details(request)
        read.assert_called_once_with("team_1", "hello-world", "pt", LEASE)
        handler._send_json.assert_called_once_with(HTTPStatus.OK, {**PUBLISHED_PT, "trace_id": "a" * 32}, no_store=True)


if __name__ == "__main__":
    unittest.main()
