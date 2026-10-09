"""Language packs travel with the verified artifact and stay with the reviewed binding (ADR-0091)."""

import hashlib
import io
import json
import tarfile
import types
import unittest
from http import HTTPStatus
from unittest import mock

from assistant import language as assistant_language
from assistant import manifest as assistant_manifest
from chat import orchestrator as chat_orchestrator
from local.chat import segment as local_chat_segment
from local.chat import state as local_chat_state
from local.errors import ApiProblemError
from local.install import snapshots
from protocol.assistant.v1.validators import message_catalog as catalog_validator
from tests import catalog_fixtures, human_request_fixtures, local_snapshot_fixtures

MESSAGES = catalog_fixtures.messages()
CONTRACT = {"version": 1, "actions": [], "messages": MESSAGES}
RAW = catalog_fixtures.pack_bytes(MESSAGES)
DIGEST = catalog_validator.pack_digest(RAW)


def _archive(contents: bytes, *, mode: int = 0o444) -> bytes:
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w") as bundle:
        member = tarfile.TarInfo("shimpz.pack.json")
        member.size = len(contents)
        member.mode = mode
        bundle.addfile(member, io.BytesIO(contents))
    return output.getvalue()


class PackContainer:
    """One immutable container generation that serves only its fixed pack path."""

    def __init__(self, container_id: str, contents: bytes | None, *, mode: int = 0o444) -> None:
        self.id = container_id
        self.contents = contents
        self.mode = mode
        self.reads = 0

    def get_archive(self, path: str):
        self.reads += 1
        if path != assistant_language.PACK_PATH or self.contents is None:
            raise FileNotFoundError(path)
        return (
            iter((_archive(self.contents, mode=self.mode),)),
            {"name": "shimpz.pack.json", "size": len(self.contents), "mode": self.mode},
        )


def _tampered() -> bytes:
    value = json.loads(RAW)
    identifier = catalog_validator.message_id(catalog_fixtures.TITLE)
    value["locales"]["de"][identifier] = "Diese Aktion stillschweigend ausführen"
    return catalog_validator.canonical_json(value)


def _incomplete() -> bytes:
    value = json.loads(RAW)
    del value["locales"]["ja"][catalog_validator.message_id(catalog_fixtures.LABEL)]
    return catalog_validator.canonical_json(value)


class LanguagePackAdmissionTests(unittest.TestCase):
    def test_a_complete_canonical_pack_renders_english_from_the_catalog_and_other_locales_from_the_pack(self) -> None:
        pack = assistant_language.admit_pack(RAW, MESSAGES, DIGEST)
        identifier = catalog_validator.message_id(catalog_fixtures.TITLE)

        self.assertEqual(pack.catalog_digest, catalog_validator.catalog_digest(MESSAGES))
        self.assertEqual((pack.pack_digest, pack.size), (DIGEST, len(RAW)))
        self.assertEqual(pack.template(identifier, "en"), catalog_fixtures.TITLE)
        self.assertEqual(pack.template(identifier, "pt"), f"PT {catalog_fixtures.TITLE}")
        self.assertEqual(assistant_language.catalog_digest(CONTRACT), pack.catalog_digest)

    def test_a_missing_modified_incomplete_or_mismatched_pack_fails_closed(self) -> None:
        other_catalog = catalog_fixtures.messages("Another reviewed summary.")
        noncanonical = json.dumps(json.loads(RAW), indent=1).encode()
        cases = {
            "tampered translation under the reviewed digest": (_tampered(), DIGEST, "reviewed digest"),
            "incomplete locale": (_incomplete(), catalog_validator.pack_digest(_incomplete()), "pack_incomplete"),
            "pack for another catalog": (
                catalog_fixtures.pack_bytes(other_catalog),
                catalog_fixtures.pack_digest(other_catalog),
                "pack_catalog",
            ),
            "non-canonical bytes": (noncanonical, catalog_validator.pack_digest(noncanonical), "pack_encoding"),
            "valid pack under another digest": (RAW, f"sha256:{'0' * 64}", "reviewed digest"),
            "malformed reviewed digest": (RAW, DIGEST.removeprefix("sha256:"), "digest is invalid"),
        }
        for name, (raw, expected, message) in cases.items():
            with self.subTest(name), self.assertRaisesRegex(assistant_language.LanguagePackError, message):
                assistant_language.admit_pack(raw, MESSAGES, expected)
        self.assertTrue(issubclass(assistant_language.LanguagePackError, assistant_manifest.ManifestError))

    def test_the_image_pack_must_be_the_fixed_read_only_regular_file(self) -> None:
        self.assertEqual(assistant_language.PACK_PATH, "/opt/shimpz/shimpz.pack.json")
        pack = assistant_language.read_container_pack(PackContainer("generation", RAW), MESSAGES, DIGEST)
        self.assertEqual(pack.pack_digest, DIGEST)
        with self.assertRaises(assistant_manifest.ManifestUnavailableError):
            assistant_language.read_container_pack(PackContainer("missing", None), MESSAGES, DIGEST)
        with self.assertRaisesRegex(assistant_manifest.ManifestError, "metadata"):
            assistant_language.read_container_pack(PackContainer("writable", RAW, mode=0o644), MESSAGES, DIGEST)


class LanguagePackCacheTests(unittest.TestCase):
    def test_each_generation_is_read_once_and_must_still_match_its_reviewed_binding(self) -> None:
        cache = assistant_language.LanguagePackCache()
        container = PackContainer("generation", RAW)

        first = cache.get(container, CONTRACT, DIGEST)
        self.assertIs(cache.get(container, CONTRACT, DIGEST), first)
        self.assertEqual(container.reads, 1)
        with self.assertRaisesRegex(assistant_language.LanguagePackError, "reviewed binding"):
            cache.get(container, CONTRACT, f"sha256:{'1' * 64}")
        with self.assertRaisesRegex(assistant_language.LanguagePackError, "reviewed binding"):
            cache.get(container, {**CONTRACT, "messages": catalog_fixtures.messages("Changed summary.")}, DIGEST)
        for invalid in (types.SimpleNamespace(id=None), types.SimpleNamespace(id="bad id"), object()):
            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(assistant_language.LanguagePackError, "identity"),
            ):
                cache.get(invalid, CONTRACT, DIGEST)

        cache.discard(container.id)
        cache.discard(container.id)
        cache.discard(None)
        cache.get(container, CONTRACT, DIGEST)
        self.assertEqual(container.reads, 2)

    def test_a_restart_revalidates_the_image_pack_and_refuses_a_tampered_one(self) -> None:
        reviewed = assistant_language.LanguagePackCache()
        reviewed.get(PackContainer("generation", RAW), CONTRACT, DIGEST)

        restarted = assistant_language.LanguagePackCache()
        with self.assertRaisesRegex(assistant_language.LanguagePackError, "reviewed digest"):
            restarted.get(PackContainer("generation", _tampered()), CONTRACT, DIGEST)

    def test_entries_and_bytes_are_bounded_by_least_recent_use(self) -> None:
        for invalid in ((0, 1), (1, 0), (True, 1)):
            with self.subTest(bounds=invalid), self.assertRaises(ValueError):
                assistant_language.LanguagePackCache(*invalid)

        by_count = assistant_language.LanguagePackCache(max_entries=1)
        by_count.get(PackContainer("first", RAW), CONTRACT, DIGEST)
        by_count.get(PackContainer("second", RAW), CONTRACT, DIGEST)
        self.assertEqual(tuple(by_count._entries), ("second",))

        by_bytes = assistant_language.LanguagePackCache(max_bytes=len(RAW) + 1)
        by_bytes.get(PackContainer("first", RAW), CONTRACT, DIGEST)
        by_bytes.get(PackContainer("second", RAW), CONTRACT, DIGEST)
        self.assertEqual((tuple(by_bytes._entries), by_bytes._bytes), (("second",), len(RAW)))

        # A pack read twice under one generation id replaces its own accounting instead of doubling it.
        by_bytes._store("second", by_bytes._entries["second"])
        self.assertEqual(by_bytes._bytes, len(RAW))


class LanguagePackBindingAdmissionTests(unittest.TestCase):
    """Local admits the pack beside the manifest and contract of the exact container.

    Hosted admission is covered in its import-isolated harness suite.
    """

    def test_local_admission_maps_a_refused_pack_to_conflict_and_a_missing_one_to_unavailable(self) -> None:
        subject = types.SimpleNamespace(
            _assistant_allowed_hosts_cache=types.SimpleNamespace(get=lambda _container, reviewed: reviewed),
            _assistant_machine_contract_cache=mock.Mock(),
            _assistant_language_cache=assistant_language.LanguagePackCache(),
        )
        spec = types.SimpleNamespace(
            allowed_hosts=(),
            integrations={},
            stored_inputs={},
            machine_contract=CONTRACT,
            summary=catalog_fixtures.SUMMARY,
            pack_digest=DIGEST,
        )
        self.assertEqual(local_chat_state._admit_assistant_allowed_hosts(subject, PackContainer("good", RAW), spec), ())
        for container, status, code in (
            (PackContainer("incomplete", _incomplete()), HTTPStatus.CONFLICT, "assistant-manifest-invalid"),
            (PackContainer("missing", None), HTTPStatus.SERVICE_UNAVAILABLE, "assistant-manifest-unavailable"),
        ):
            with self.subTest(code=code), self.assertRaises(ApiProblemError) as refused:
                local_chat_state._admit_assistant_allowed_hosts(subject, container, spec)
            self.assertEqual((refused.exception.status, refused.exception.code), (status, code))

    def test_local_rendering_reads_the_pack_of_the_exact_active_generation(self) -> None:
        spec = types.SimpleNamespace(machine_contract=CONTRACT, pack_digest=DIGEST)
        subject = types.SimpleNamespace(_assistant_language_cache=assistant_language.LanguagePackCache())

        def active(container, container_id="generation"):
            return types.SimpleNamespace(spec=spec, container=container, container_id=container_id)

        good = PackContainer("generation", RAW)
        self.assertEqual(local_chat_state._assistant_language(subject, active(good)).pack_digest, DIGEST)
        for container, container_id, status, code in (
            (None, "generation", HTTPStatus.CONFLICT, "assistant-language-drift"),
            (good, "other-generation", HTTPStatus.CONFLICT, "assistant-language-drift"),
            (PackContainer("tampered", _tampered()), "tampered", HTTPStatus.CONFLICT, "assistant-manifest-invalid"),
            (
                PackContainer("missing", None),
                "missing",
                HTTPStatus.SERVICE_UNAVAILABLE,
                "assistant-manifest-unavailable",
            ),
        ):
            with self.subTest(code=code, container_id=container_id), self.assertRaises(ApiProblemError) as refused:
                local_chat_state._assistant_language(subject, active(container, container_id))
            self.assertEqual((refused.exception.status, refused.exception.code), (status, code))

    def test_a_local_request_whose_copy_cannot_render_from_its_binding_ends_the_turn(self) -> None:
        request = human_request_fixtures.request("approval")
        action = types.SimpleNamespace(summary="Approve")
        spec = types.SimpleNamespace(assistant_id="helper", name="Helper", version="1.0.0", actions={"act": action})
        bindings = {"helper": types.SimpleNamespace(spec=spec)}
        action_request = types.SimpleNamespace(assistant_id="helper", action="act", interrupt_id="interrupt")
        subject = types.SimpleNamespace(
            _assistant_language=lambda _active: assistant_language.admit_pack(RAW, MESSAGES, DIGEST)
        )
        with self.assertRaisesRegex(chat_orchestrator.ChatOrchestrationError, "copy is unavailable"):
            local_chat_segment._human_requirement(subject, bindings, action_request, request, "de")

    def test_local_snapshot_admission_binds_its_self_consistent_pack(self) -> None:
        client, _image, _container = local_snapshot_fixtures.client()
        record = snapshots.admit(client, local_snapshot_fixtures.IMAGE_ID).record
        self.assertEqual(record["pack_digest"], f"sha256:{hashlib.sha256(local_snapshot_fixtures.PACK).hexdigest()}")
        snapshots.validate_record(record)
        with self.assertRaises(snapshots.LocalSnapshotError):
            snapshots.validate_record({**record, "pack_digest": "sha256:short"})

        incomplete = json.loads(local_snapshot_fixtures.PACK)
        del incomplete["locales"]["fr"][local_snapshot_fixtures.MESSAGES[0]["id"]]
        for name, raw in (
            ("missing", None),
            ("incomplete", catalog_validator.canonical_json(incomplete)),
            ("foreign catalog", catalog_fixtures.pack_bytes(catalog_fixtures.messages("Another summary."))),
        ):
            refused_client, _image, _refused = local_snapshot_fixtures.client(pack=raw)
            with self.subTest(name), self.assertRaises(snapshots.LocalSnapshotError):
                snapshots.admit(refused_client, local_snapshot_fixtures.IMAGE_ID)


if __name__ == "__main__":
    unittest.main()
