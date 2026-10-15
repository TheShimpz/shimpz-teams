import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from inference import config as inference_config
from tests import private_state_fixtures


class InferenceConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name) / "inference"
        self.store = inference_config.InferenceConfigStore(self.root)

    def test_only_exact_catalog_values_are_admitted_without_defaults(self):
        for provider, model in (
            (None, "gpt-6-luna"),
            ("", "gpt-6-luna"),
            ("OpenAI", "gpt-6-luna"),
            (" openai", "gpt-6-luna"),
            ("openai", None),
            ("openai", ""),
            ("openai", "gpt-6-luna "),
            ("openai", 7),
        ):
            with self.subTest(provider=provider, model=model), self.assertRaises(inference_config.InferenceConfigError):
                inference_config.normalize(provider, model)
        self.assertNotIn("image", inference_config.PROVIDERS["openai"])

    def test_exact_provider_catalog_is_accepted(self):
        expected = {
            provider["id"]: {model["id"] for model in provider["models"]}
            for provider in inference_config._MODEL_CATALOG["providers"]
        }

        self.assertEqual(
            {provider: set(definition["models"]) for provider, definition in inference_config.PROVIDERS.items()},
            expected,
        )
        for provider, models in expected.items():
            for model in models:
                with self.subTest(provider=provider, model=model):
                    self.assertEqual(inference_config.normalize(provider, model).model, model)

    def test_save_and_load_preserve_only_provider_and_model(self):
        expected = inference_config.normalize("anthropic", "claude-sonnet-5-5")

        self.store.save("team_1", expected)
        actual = self.store.load("team_1")

        self.assertEqual(actual, expected)
        files = list(self.root.glob("*.json"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.root.stat().st_mode & 0o777, 0o700)
        self.assertNotIn(b"api_key", files[0].read_bytes())
        self.assertEqual(
            json.loads(files[0].read_bytes()),
            {
                "schema": inference_config.SCHEMA,
                "team_id": "team_1",
                "provider": "anthropic",
                "model": "claude-sonnet-5-5",
            },
        )

    def test_team_ids_follow_the_team_protocol_grammar(self) -> None:
        config = inference_config.normalize("openai", "gpt-6-luna")
        for team_id in ("Team_1", "team-1", "t" * 41, "", None, 1):
            for operation in (
                lambda value: self.store.save(value, config),
                self.store.load,
                self.store.load_knowledge,
                self.store.delete,
            ):
                with self.subTest(team_id=team_id), self.assertRaisesRegex(inference_config.InferenceConfigError, "id"):
                    operation(team_id)
        self.assertFalse(self.root.exists())

    def test_replace_is_atomic_and_delete_is_idempotent(self):
        self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
        self.store.save("team_1", inference_config.normalize("anthropic", "claude-sonnet-5-5"))

        self.assertEqual(self.store.load("team_1").provider, "anthropic")
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        self.store.delete("team_1")
        self.store.delete("team_1")
        with self.assertRaises(inference_config.InferenceConfigError):
            self.store.load("team_1")

    def test_saved_records_commit_their_directory_entry(self):
        real_fsync = os.fsync
        with private_state_fixtures.observed_fsyncs() as synced:
            self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
        self.assertEqual(synced, ["file", "directory"])

        def fail_directory(descriptor: int) -> None:
            if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise OSError("directory sync")
            real_fsync(descriptor)

        with (
            mock.patch.object(os, "fsync", side_effect=fail_directory),
            self.assertRaisesRegex(OSError, "directory sync"),
        ):
            self.store.save("team_1", inference_config.normalize("anthropic", "claude-sonnet-5-5"))
        self.assertEqual(list(self.root.glob(".*.tmp")), [])

    def test_unknown_cross_provider_and_unsafe_models_fail_closed(self):
        with self.assertRaises(inference_config.InferenceConfigError):
            inference_config.normalize("unknown-provider", "gpt-test")
        for provider, model in (
            ("openai", "gpt-999"),
            ("openai", "claude-sonnet-5-5"),
            ("anthropic", "gpt-6.1-sol"),
            ("openai", "../../model"),
        ):
            with self.subTest(provider=provider, model=model), self.assertRaises(inference_config.InferenceConfigError):
                inference_config.normalize(provider, model)
        with self.assertRaises(inference_config.InferenceConfigError):
            self.store.save("../team", inference_config.normalize("openai", "gpt-6-luna"))

    def test_persisted_model_outside_the_catalog_fails_closed(self):
        self.root.mkdir(parents=True)
        self.store._path("team_1").write_text(
            json.dumps(
                {"schema": inference_config.SCHEMA, "team_id": "team_1", "provider": "openai", "model": "gpt-999"}
            ),
            encoding="utf-8",
        )
        with self.assertRaises(inference_config.InferenceConfigError):
            self.store.load("team_1")

    def test_malformed_and_cross_team_persisted_metadata_fails_closed(self) -> None:
        self.root.mkdir(parents=True)
        target = self.store._path("team_1")
        for payload in (
            b"\xff",
            b"{",
            b"[]",
            json.dumps({"schema": inference_config.SCHEMA}).encode(),
            json.dumps(
                {
                    "schema": inference_config.SCHEMA + 1,
                    "team_id": "team_1",
                    "provider": "openai",
                    "model": "gpt-6.1-sol",
                }
            ).encode(),
            json.dumps(
                {
                    "schema": inference_config.SCHEMA,
                    "team_id": "team_2",
                    "provider": "openai",
                    "model": "gpt-6.1-sol",
                }
            ).encode(),
        ):
            with self.subTest(payload=payload):
                target.write_bytes(payload)
                with self.assertRaisesRegex(inference_config.InferenceConfigError, "invalid"):
                    self.store.load("team_1")

    def test_only_absent_configuration_is_reported_as_missing(self) -> None:
        with self.assertRaises(inference_config.InferenceConfigMissingError):
            self.store.load("team_1")
        self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
        path = next(self.root.iterdir())
        for payload in (
            {"schema": inference_config.SCHEMA, "team_id": "team_1", "provider": "openai"},
            {
                "schema": inference_config.SCHEMA,
                "team_id": "team_1",
                "provider": "openai",
                "model": "gpt-6.1-sol",
                "x": 0,
            },
        ):
            with self.subTest(payload=payload):
                path.write_text(json.dumps(payload), encoding="utf-8")
                with self.assertRaises(inference_config.InferenceConfigError) as caught:
                    self.store.load("team_1")
                self.assertNotIsInstance(caught.exception, inference_config.InferenceConfigMissingError)
        with (
            mock.patch("storage.private_state.os.open", side_effect=PermissionError("denied")),
            self.assertRaises(inference_config.InferenceConfigError) as caught,
        ):
            self.store.load("team_1")
        self.assertNotIsInstance(caught.exception, inference_config.InferenceConfigMissingError)

    def test_deletions_commit_their_directory_entries_and_an_absent_store_is_already_deleted(self) -> None:
        self.store.delete("team_1")
        self.store.delete_all()
        self.assertFalse(self.root.exists())
        self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
        with private_state_fixtures.observed_fsyncs() as synced:
            self.store.delete("team_1")
        self.assertEqual(synced, ["directory", "directory"])
        self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
        with private_state_fixtures.observed_fsyncs() as synced:
            self.store.delete_all()
        self.assertEqual(synced, ["directory"])
        self.assertEqual(list(self.root.iterdir()), [])
        self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))
        for operation in (lambda: self.store.delete("team_1"), self.store.delete_all):
            with (
                self.subTest(operation=operation),
                mock.patch.object(os, "fsync", side_effect=OSError("directory sync")),
                self.assertRaisesRegex(inference_config.InferenceConfigError, "could not be removed"),
            ):
                operation()
            self.store.save("team_1", inference_config.normalize("openai", "gpt-6-luna"))

    def test_delete_wraps_filesystem_failure(self) -> None:
        with (
            mock.patch.object(Path, "unlink", side_effect=OSError("read-only")),
            self.assertRaisesRegex(inference_config.InferenceConfigError, "could not be removed"),
        ):
            self.store.delete("team_1")


if __name__ == "__main__":
    unittest.main()
