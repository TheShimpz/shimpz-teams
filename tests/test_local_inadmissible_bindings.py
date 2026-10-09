"""An intact binding the current Assistant contract refuses is taken out of service for that Assistant only.

A binding whose stored document the current contract refuses stays owned and listed as needing replacement; every
runtime use of it is refused, and nothing about it can stop the Team from starting (ADR-0033's 2026-10-08 amendment).
Only integrity stays fatal: a binding Team did not write is never trusted.
"""

import copy
import json
import logging
import tempfile
import types
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest import mock

from docker.errors import DockerException
from local_controller_harness import LocalContractCase
from test_local_publication_install import _runtime_resolution
from test_local_snapshots import IMAGE_ID, _client

from install import bindings
from install import update as assistant_update
from install.contract import CONTRACT_ROOT
from local import lifecycle as local_lifecycle
from local.assistant import api as assistant_api
from local.assistant import lifecycle as assistant_lifecycle
from local.assistant import resources as local_resources
from local.errors import ApiProblemError
from local.install import automatic, service, snapshots
from local.install.registry import AssistantRegistry
from local.labels import ASSISTANT_LABEL

RESOLUTION = json.loads((CONTRACT_ROOT / "vectors.json").read_bytes())["fixtures"]["resolve_response"]["value"]
# The current contract bounds the Assistant summary to 80 characters.
_REFUSED_SUMMARY = "s" * 81


def _refuse(path: Path, team_id: str, assistant_id: str, change) -> None:
    """Rewrite one stored binding so that the current contract refuses it, with its integrity intact."""
    stored = json.loads(path.read_bytes())
    for value in stored["bindings"]:
        name = "resolution" if value["provenance"] == "published" else "local_record"
        if value["team_id"] == team_id and value[name]["assistant_id"] == assistant_id:
            change(value[name])
            value["binding_digest"] = bindings._binding(team_id, value["provenance"], name, value[name]).binding_digest
    path.write_text(json.dumps(stored), encoding="utf-8")


def _summary(document: dict[str, object]) -> None:
    document["summary"] = _REFUSED_SUMMARY


def _summary_bound_beyond_the_contract(record: dict[str, object]) -> None:
    """A Local record whose summary message admits more characters than the current contract bounds."""
    for message in record["machine_contract"]["messages"]:
        if message["msgid"] == record["summary"]:
            message["max_length"] = 81


class InadmissibleBindingStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "bindings.json"

    def test_a_refused_publication_is_kept_intact_and_refused_on_every_use(self) -> None:
        store = DynamicAssistantStoreFactory.published(self.path)
        _refuse(self.path, "team_1", "helper", _summary)

        admitted, refused = sorted(store.snapshot(), key=lambda binding: binding.assistant_id)
        self.assertTrue(admitted.admissible)
        self.assertFalse(refused.admissible)
        self.assertEqual(refused.document["summary"], _REFUSED_SUMMARY)
        with self.assertRaises(bindings.InadmissibleAssistantBindingError):
            _ = refused.resolution
        with self.assertRaisesRegex(bindings.DynamicAssistantError, "not a local snapshot"):
            _ = refused.local_record

    def test_a_local_record_with_an_unbounded_summary_is_refused_and_never_fatal(self) -> None:
        client, _image, _container = _client()
        record = snapshots.admit(client, IMAGE_ID).record
        store = bindings.DynamicAssistantStore(self.path, local_record_validator=snapshots.validate_record)
        store.put_local("team_1", record)
        _refuse(self.path, "team_1", record["assistant_id"], _summary_bound_beyond_the_contract)

        (refused,) = store.snapshot()
        self.assertFalse(refused.admissible)
        with self.assertRaises(bindings.InadmissibleAssistantBindingError):
            _ = refused.local_record

    def test_a_local_record_without_its_page_copy_is_refused_and_still_uninstallable(self) -> None:
        client, _image, _container = _client()
        record = snapshots.admit(client, IMAGE_ID).record
        store = bindings.DynamicAssistantStore(self.path, local_record_validator=snapshots.validate_record)
        store.put_local("team_1", record)

        def without_page_copy(document: dict[str, object]) -> None:
            for field in ("description", "links", "declared_creators"):
                del document[field]

        _refuse(self.path, "team_1", record["assistant_id"], without_page_copy)
        (refused,) = store.snapshot()
        self.assertFalse(refused.admissible)
        self.assertNotIn("description", refused.document)
        self.assertTrue(store.delete_if_matches("team_1", refused.assistant_id, refused.binding_digest))
        self.assertEqual(store.snapshot(), ())

    def test_integrity_stays_fatal_for_the_whole_registry(self) -> None:
        store = DynamicAssistantStoreFactory.published(self.path)
        stored = json.loads(self.path.read_bytes())
        stored["bindings"][0]["resolution"]["summary"] = _REFUSED_SUMMARY
        self.path.write_text(json.dumps(stored), encoding="utf-8")
        with self.assertRaisesRegex(bindings.DynamicAssistantError, "digest is invalid"):
            store.snapshot()

    def test_registry_exposes_a_refused_binding_only_as_needing_replacement(self) -> None:
        registry = AssistantRegistry(DynamicAssistantStoreFactory.published(self.path))
        _refuse(self.path, "team_1", "helper", _summary)

        admitted, refused = registry.installed("team_1")
        self.assertEqual([binding.assistant_id for binding in admitted], ["current"])
        self.assertEqual([binding.assistant_id for binding in refused], ["helper"])
        self.assertEqual(registry.team_bindings("team_1"), admitted)
        self.assertEqual(registry.inadmissible(), refused)
        self.assertEqual([spec.assistant_id for spec in registry.catalog()], ["current"])
        with self.assertRaises(bindings.InadmissibleAssistantBindingError):
            registry.get("team_1", "helper")
        with self.assertRaises(bindings.InadmissibleAssistantBindingError):
            registry.spec(refused[0])
        # Its image stays held, so nothing collects it before the Supervisor replaces or uninstalls the Assistant.
        self.assertEqual(
            registry.images(),
            tuple(sorted({admitted[0].document["image_reference"], refused[0].document["image_reference"]})),
        )

    def test_a_binding_whose_runtime_contract_is_refused_needs_replacement_too(self) -> None:
        # The install protocol admits this publication, but its open Action schemas fail the Team runtime contract.
        store = bindings.DynamicAssistantStore(self.path)
        store.put("team_1", copy.deepcopy(RESOLUTION))
        registry = AssistantRegistry(store)

        refused = registry.binding("team_1", RESOLUTION["assistant_id"])
        self.assertFalse(refused.admissible)
        self.assertEqual(registry.installed("team_1"), ((), (refused,)))
        self.assertEqual(registry.inadmissible(), (refused,))
        self.assertEqual(registry.catalog(), ())
        with self.assertRaises(bindings.InadmissibleAssistantBindingError):
            registry.get("team_1", RESOLUTION["assistant_id"])

    def test_a_refused_binding_without_an_image_reference_defers_image_collection(self) -> None:
        registry = AssistantRegistry(DynamicAssistantStoreFactory.published(self.path))
        _refuse(self.path, "team_1", "helper", lambda document: document.pop("image_reference"))
        with self.assertRaisesRegex(bindings.DynamicAssistantError, "holds no image reference"):
            registry.images()

    def test_update_transactions_over_a_refused_binding_are_recovered_per_assistant(self) -> None:
        refused = types.SimpleNamespace(team_id="team_1", assistant_id="refused")
        update = types.SimpleNamespace(team_id="team_1", assistant_id="refused", previous=refused, successor=object())
        subject = types.SimpleNamespace(
            updates=types.SimpleNamespace(list=lambda: (update,)),
            _lock=lambda _team_id: nullcontext(),
            registry=types.SimpleNamespace(
                binding=lambda *_args: refused,
                spec=mock.Mock(side_effect=bindings.InadmissibleAssistantBindingError("replace")),
            ),
            _recover_update_target=mock.Mock(),
            sweep_residues=mock.Mock(),
        )
        with self.assertLogs(assistant_lifecycle.log, logging.ERROR):
            assistant_lifecycle.recover_updates(subject)
        subject._recover_update_target.assert_not_called()
        subject.sweep_residues.assert_called_once_with()

    def test_update_store_refuses_a_transaction_filed_under_another_identity(self) -> None:
        store = DynamicAssistantStoreFactory.published(self.path)
        previous = store.get("team_1", "helper")
        successor = copy.deepcopy(previous.resolution)
        successor["assistant_version"] = "9.0.0"
        updates = assistant_update.AssistantUpdateStore(self.path.parent / "updates")
        updates.begin(previous, successor, "sha256:" + "a" * 64)
        filed = next((self.path.parent / "updates").glob("*.json"))
        misfiled = filed.with_name("team_2--helper.json")
        filed.rename(misfiled)
        with self.assertRaisesRegex(bindings.DynamicAssistantError, "filename is invalid"):
            updates.get("team_2", "helper")
        self.assertTrue(misfiled.exists())

    def test_update_store_keeps_a_refused_transaction_readable(self) -> None:
        store = DynamicAssistantStoreFactory.published(self.path)
        previous = store.get("team_1", "helper")
        successor = copy.deepcopy(previous.resolution)
        successor["assistant_version"] = "9.0.0"
        updates = assistant_update.AssistantUpdateStore(self.path.parent / "updates")
        updates.begin(previous, successor, "sha256:" + "a" * 64)
        transaction = next((self.path.parent / "updates").glob("*.json"))
        stored = json.loads(transaction.read_bytes())
        stored["successor"]["resolution"]["summary"] = _REFUSED_SUMMARY
        successor_binding = stored["successor"]
        successor_binding["binding_digest"] = bindings._binding(
            "team_1", "published", "resolution", successor_binding["resolution"]
        ).binding_digest
        transaction.write_text(json.dumps(stored), encoding="utf-8")

        (recovered,) = updates.list()
        self.assertTrue(recovered.previous.admissible)
        self.assertFalse(recovered.successor.admissible)


class DynamicAssistantStoreFactory:
    @staticmethod
    def published(path: Path) -> bindings.DynamicAssistantStore:
        """Bind an admitted ``current`` and ``helper`` publication to team_1."""
        store = bindings.DynamicAssistantStore(path)
        for assistant_id in ("current", "helper"):
            resolution = _runtime_resolution()
            resolution["assistant_id"] = assistant_id
            store.put("team_1", resolution)
        return store


def _refused(assistant_id: str = "shimpz-cloudflare", provenance: str = "published", **document: object):
    return types.SimpleNamespace(
        team_id="team_1",
        assistant_id=assistant_id,
        binding_digest="sha256:" + "c" * 64,
        admissible=False,
        provenance=provenance,
        document={"assistant_version": "0.5.2", **document},
    )


class InadmissibleBindingLifecycleTests(LocalContractCase):
    def test_startup_quarantine_removes_each_runtime_and_isolates_failures(self) -> None:
        controller, container, events = self._lifecycle_controller()
        lifecycle = controller.assistant_lifecycle
        broken = _refused("broken")
        refused = _refused()
        lifecycle._remove_egress_policy = lambda team_id, assistant_id: events.append(("revoke", assistant_id))
        lifecycle._blocked_action_workloads.add(container.id)

        def container_for(_team_id, assistant_id, **_kwargs):
            if assistant_id == "broken":
                raise DockerException("unavailable")
            return container

        lifecycle._assistant_container = container_for
        controller.registry.inadmissible = lambda: (broken, refused)

        with self.assertLogs(assistant_lifecycle.log, logging.WARNING) as logs:
            lifecycle.quarantine_inadmissible()

        # Each refused Assistant's egress is revoked before, and regardless of, the removal of its runtime.
        self.assertEqual(
            events,
            [
                ("revoke", "broken"),
                ("revoke", "shimpz-cloudflare"),
                ("remove", True),
                ("residue-add", container.attrs["Image"]),
            ],
        )
        self.assertNotIn(container.id, lifecycle._blocked_action_workloads)
        self.assertTrue(any("deferred for team_1/broken" in line for line in logs.output))

        events.clear()
        lifecycle._remove_egress_policy = mock.Mock(side_effect=ApiProblemError(503, "policy", code="egress-policy"))
        controller.registry.inadmissible = lambda: (refused,)
        with self.assertLogs(assistant_lifecycle.log, logging.ERROR) as logs:
            lifecycle.quarantine_inadmissible()
        self.assertIn(("remove", True), events)
        self.assertTrue(any("revocation deferred for team_1/shimpz-cloudflare" in line for line in logs.output))

    def test_quarantine_keeps_a_container_it_cannot_prove_and_a_local_image(self) -> None:
        controller, container, events = self._lifecycle_controller()
        lifecycle = controller.assistant_lifecycle
        lifecycle._remove_egress_policy = mock.Mock()
        controller.registry.inadmissible = lambda: (_refused("other-assistant"),)
        with self.assertLogs(assistant_lifecycle.log, logging.ERROR):
            lifecycle.quarantine_inadmissible()
        self.assertNotIn(("remove", True), events)
        lifecycle._remove_egress_policy.assert_called_once_with("team_1", "other-assistant")

        controller.registry.inadmissible = lambda: (_refused(provenance="local"),)
        lifecycle.quarantine_inadmissible()
        self.assertIn(("remove", True), events)
        self.assertFalse(any(isinstance(event, tuple) and event[0] == "residue-add" for event in events))

        events.clear()
        container.attrs = {**container.attrs, "Image": "unnamed"}
        controller.registry.inadmissible = lambda: (_refused(),)
        lifecycle.quarantine_inadmissible()
        self.assertEqual(events, [("remove", True)])

        lifecycle._assistant_container = lambda *_args, **_kwargs: None
        lifecycle.quarantine_inadmissible()
        lifecycle._remove_egress_policy.assert_called_with("team_1", "shimpz-cloudflare")

        controller.registry.inadmissible = mock.Mock(side_effect=bindings.DynamicAssistantError("unavailable"))
        with self.assertLogs(assistant_lifecycle.log, logging.ERROR):
            lifecycle.quarantine_inadmissible()

    def test_a_quarantine_that_proves_nothing_is_logged_and_never_stops_the_team(self) -> None:
        controller, _container, events = self._lifecycle_controller()
        lifecycle = controller.assistant_lifecycle
        lifecycle._remove_egress_policy = mock.Mock(side_effect=ApiProblemError(503, "policy", code="egress-policy"))
        lifecycle._assistant_container = mock.Mock(side_effect=DockerException("unavailable"))
        controller.registry.inadmissible = lambda: (_refused(),)
        with self.assertLogs(assistant_lifecycle.log, logging.ERROR):
            lifecycle.quarantine_inadmissible()
        self.assertEqual(events, [])

    def test_startup_resumes_only_admitted_bindings(self) -> None:
        subject = types.SimpleNamespace(
            registry=types.SimpleNamespace(
                identities=lambda: {("team_1", "shimpz-cloudflare"), ("team_1", "current")},
                inadmissible=lambda: (_refused(),),
            ),
            install_assistant=mock.Mock(),
        )
        assistant_lifecycle.resume_assistants(subject)
        subject.install_assistant.assert_called_once_with("team_1", "current")

    def test_uninstall_removes_a_refused_runtime_by_its_ownership_labels(self) -> None:
        controller, container, events = self._lifecycle_controller()
        lifecycle = controller.assistant_lifecycle
        refused = _refused()
        remaining = [container]
        container.remove = lambda *, force: (events.append(("remove", force)), remaining.clear())
        lifecycle._assistant_container = lambda *_args, **_kwargs: remaining[0] if remaining else None
        lifecycle._egress_token = mock.Mock(return_value=None)
        controller.registry.binding = lambda *_args: refused
        controller.registry.bindings = lambda: ()
        lifecycle.icons = types.SimpleNamespace(retire=lambda _binding, _references, delete: delete())
        # An update the refused binding interrupted can never be recovered: it is retired with its Assistant.
        interrupted = types.SimpleNamespace(previous=refused, previous_image_id="sha256:" + "d" * 64)
        lifecycle.updates = types.SimpleNamespace(
            get=mock.Mock(return_value=interrupted), clear=mock.Mock(side_effect=lambda _update: None)
        )
        lifecycle.residues = types.SimpleNamespace(add=mock.Mock())

        result = lifecycle.uninstall_assistant("team_1", "shimpz-cloudflare")

        self.assertEqual(result, {"assistant": "shimpz-cloudflare", "uninstalled": False})
        self.assertIn(("remove", True), events)
        lifecycle.residues.add.assert_called_once_with("sha256:" + "d" * 64)
        self.assertIn(("residue-add", container.attrs["Image"]), events)
        lifecycle.updates.clear.assert_called_once_with(interrupted)

        # The transaction stays the retry anchor until its previous image is durably queued.
        lifecycle.updates.clear.reset_mock()
        remaining.append(container)
        for failure in (OSError("full"), bindings.DynamicAssistantError("unavailable")):
            lifecycle.residues.add = mock.Mock(side_effect=failure)
            with self.subTest(failure=failure), self.assertRaises(ApiProblemError) as caught:
                lifecycle.uninstall_assistant("team_1", "shimpz-cloudflare")
            self.assertEqual(caught.exception.code, "assistant-update-unavailable")
        lifecycle.updates.clear.assert_not_called()
        remaining.clear()
        # A local predecessor's staged image is never queued for collection.
        lifecycle.residues.add = mock.Mock()
        interrupted.previous = _refused(provenance="local")
        lifecycle.uninstall_assistant("team_1", "shimpz-cloudflare")
        lifecycle.residues.add.assert_not_called()

        lifecycle.updates.get = mock.Mock(side_effect=bindings.DynamicAssistantError("unavailable"))
        remaining.append(container)
        with self.assertRaises(ApiProblemError) as caught:
            lifecycle.uninstall_assistant("team_1", "shimpz-cloudflare")
        self.assertEqual(caught.exception.code, "assistant-update-unavailable")
        self.assertIs(remaining[0], container)
        lifecycle.updates.get = mock.Mock(return_value=None)

        remaining.append(container)
        container.remove = mock.Mock(side_effect=DockerException("busy"))
        with self.assertRaises(ApiProblemError) as caught:
            lifecycle.uninstall_assistant("team_1", "shimpz-cloudflare")
        self.assertEqual(caught.exception.code, "docker-remove-failed")

    def test_replacement_uninstalls_the_refused_binding_then_installs_fresh(self) -> None:
        controller, _container, _events = self._lifecycle_controller()
        lifecycle = controller.assistant_lifecycle
        refused = _refused()
        controller.registry.binding = lambda *_args: refused
        lifecycle._uninstall_assistant_unguarded = mock.Mock()
        install_successor = mock.Mock(return_value={"installed": True})

        self.assertEqual(lifecycle.replace_inadmissible("team_1", refused, install_successor), {"installed": True})
        lifecycle._uninstall_assistant_unguarded.assert_called_once_with("team_1", "shimpz-cloudflare")
        install_successor.assert_called_once_with(lifecycle._install_assistant_unguarded)

        for current in (
            None,
            _refused(provenance="local"),
            types.SimpleNamespace(**{**vars(refused), "admissible": True}),
        ):
            controller.registry.binding = lambda *_args, current=current: current
            with self.subTest(current=current), self.assertRaises(ApiProblemError) as caught:
                lifecycle.replace_inadmissible("team_1", refused, install_successor)
            self.assertEqual(caught.exception.code, "assistant-binding-conflict")


class InadmissibleBindingRequestTests(unittest.TestCase):
    @staticmethod
    def _inventory(refused: tuple[object, ...], containers: list[object]) -> types.SimpleNamespace:
        return types.SimpleNamespace(
            _lock=lambda _team_id: nullcontext(),
            assistant_lifecycle=types.SimpleNamespace(
                _network=lambda _team_id: object(),
                _assistant_filters=lambda _team_id: {},
            ),
            client=types.SimpleNamespace(containers=types.SimpleNamespace(list=lambda **_kwargs: containers)),
            registry=types.SimpleNamespace(installed=lambda _team_id: ((), refused)),
        )

    def test_inventory_lists_a_refused_binding_as_invalid_without_validating_its_runtime(self) -> None:
        leftover = types.SimpleNamespace(labels={ASSISTANT_LABEL: "shimpz-cloudflare"}, status="running")
        controller = self._inventory((_refused(),), [leftover])
        self.assertEqual(
            assistant_api.list_assistants(controller, "team_1"),
            {
                "assistants": [
                    {
                        "assistant": "shimpz-cloudflare",
                        "assistant_version": "0.5.2",
                        "status": "invalid",
                        "provenance": "published",
                    }
                ]
            },
        )
        stranger = types.SimpleNamespace(labels={ASSISTANT_LABEL: "unknown"}, status="running")
        with self.assertRaises(ApiProblemError) as caught:
            assistant_api.list_assistants(self._inventory((_refused(),), [stranger]), "team_1")
        self.assertEqual(caught.exception.code, "assistant-registry-drift")

        for version in (None, "not-a-version"):
            malformed = _refused()
            malformed.document["assistant_version"] = version
            with self.subTest(version=version), self.assertRaises(ApiProblemError) as caught:
                assistant_api.list_assistants(self._inventory((malformed,), []), "team_1")
            self.assertEqual(caught.exception.code, "assistant-registry-drift")

    def test_summary_resolution_and_egress_refuse_a_refused_binding(self) -> None:
        refusal = bindings.InadmissibleAssistantBindingError("replace")
        controller = types.SimpleNamespace(
            _lock=lambda _team_id: nullcontext(),
            registry=types.SimpleNamespace(binding=lambda *_args: _refused(), get=mock.Mock(side_effect=refusal)),
        )
        for call in (
            lambda: assistant_api.assistant_summary(controller, "team_1", "shimpz-cloudflare", "en"),
            lambda: local_resources._resolve(controller, "team_1", "shimpz-cloudflare"),
        ):
            with self.subTest(call=call), self.assertRaises(ApiProblemError) as caught:
                call()
            self.assertEqual(caught.exception.code, "assistant-manifest-invalid")

    def test_installation_replaces_a_refused_binding_and_never_updates_it_automatically(self) -> None:
        refused = _refused()
        controller = types.SimpleNamespace(
            registry=types.SimpleNamespace(binding=lambda *_args: refused, bindings=lambda: ()),
            assistant_lifecycle=types.SimpleNamespace(replace_inadmissible=mock.Mock(return_value={"installed": True})),
            developers=types.SimpleNamespace(resolve=lambda _digest: {"assistant_id": "shimpz-cloudflare"}),
            assistant_icons=types.SimpleNamespace(retained=lambda *_args: nullcontext()),
            _discard_icon=mock.Mock(),
        )
        with mock.patch.object(service, "_verify_publication_assets", return_value=b"icon"):
            result = service.install_publication(controller, "team_1", "shimpz-cloudflare", "sha256:" + "a" * 64)
        self.assertEqual(result, {"installed": True})
        controller.assistant_lifecycle.replace_inadmissible.assert_called_once_with("team_1", refused, mock.ANY)

        with self.assertRaises(ApiProblemError) as caught:
            service.install_publication(
                controller,
                "team_1",
                "shimpz-cloudflare",
                "sha256:" + "a" * 64,
                expected_binding_digest=refused.binding_digest,
            )
        self.assertEqual(caught.exception.code, "assistant-update-conflict")

        updater = object.__new__(automatic.AutomaticAssistantUpdater)
        updater._controller = types.SimpleNamespace(
            assistant_lifecycle=types.SimpleNamespace(sweep_residues=lambda: None),
            local_snapshot_collector=types.SimpleNamespace(collect=lambda: None),
            registry=types.SimpleNamespace(bindings=lambda: (refused,)),
        )
        updater._failures, updater._retry_after = {}, {}
        updater._clock = lambda: 0
        updater._record_result = mock.Mock()
        with (
            mock.patch.object(
                automatic, "_binding_version", side_effect=bindings.InadmissibleAssistantBindingError("x")
            ),
            self.assertLogs(automatic.log, logging.ERROR),
        ):
            updater.run_once()
        updater._record_result.assert_called_once_with("team_1", "shimpz-cloudflare", "error", "binding:invalid")

    def test_local_staging_replaces_a_refused_binding(self) -> None:
        refused = _refused(provenance="local")
        client, _image, _container = _client()
        admitted = snapshots.admit(client, IMAGE_ID)
        lifecycle = types.SimpleNamespace(
            replace_inadmissible=mock.Mock(
                side_effect=lambda _team_id, _previous, install: install(mock.Mock(return_value={"installed": True}))
            )
        )
        controller = types.SimpleNamespace(
            registry=types.SimpleNamespace(binding=lambda *_args: refused, bindings=lambda: ()),
            assistant_lifecycle=lifecycle,
            assistant_icons=types.SimpleNamespace(retained_local=lambda *_args: nullcontext()),
        )
        with (
            mock.patch.object(service, "_admit_local_snapshot", return_value=admitted),
            mock.patch.object(service, "_apply_local_snapshot", return_value={"installed": True}) as apply_local,
        ):
            result = service.install_local_snapshot(controller, "team_1", IMAGE_ID)
        self.assertTrue(result["installed"])
        lifecycle.replace_inadmissible.assert_called_once()
        apply_local.assert_called_once()

    def test_team_destruction_and_space_reset_release_a_refused_binding(self) -> None:
        refused = _refused()
        subject = types.SimpleNamespace(
            registry=types.SimpleNamespace(
                identities=lambda: {("team_1", "shimpz-cloudflare")},
                binding=lambda *_args: refused,
            ),
            assistant_lifecycle=types.SimpleNamespace(
                _remove_egress_policy=mock.Mock(),
                sweep_residues=mock.Mock(),
            ),
        )
        with mock.patch.object(local_lifecycle, "_retire_team_binding") as retire:
            self.assertEqual(local_lifecycle._remove_team_assistants(subject, "team_1", []), 0)
        subject.assistant_lifecycle._remove_egress_policy.assert_called_once_with("team_1", "shimpz-cloudflare")
        retire.assert_called_once_with(subject, "team_1", "shimpz-cloudflare")


if __name__ == "__main__":
    unittest.main()
