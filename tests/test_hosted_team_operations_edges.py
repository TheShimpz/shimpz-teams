"""Create, destroy, status, and runtime operation edges for Hosted Teams."""

from __future__ import annotations

import contextlib
import sys
import tempfile
import unittest
from http import HTTPStatus
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hosted_assistant_fixture as harness

from hosted.team import postgresql as postgresql_client

lifecycle = harness.hosted_lifecycle
resources = harness.hosted_resources
state = harness.runtime_state

TEAM_ID = "team_1"
OWNER = "account_1"
RUNTIME_ID = "a" * 64


def _container(**changes):
    values = {
        "id": RUNTIME_ID,
        "name": "team",
        "labels": {"team.id": TEAM_ID, "team.name": "Team", "team.owner": OWNER, "team.runtime": "1"},
        "attrs": {},
        "status": "running",
        "reload": mock.Mock(),
        "logs": mock.Mock(return_value=b"logs"),
    }
    values.update(changes)
    return SimpleNamespace(**values)


class _PostgreSQLService:
    """The Service authority as Team observes it through `_call`: provisioning fences and the absence proof.

    `clock` is the one host clock both sides read. A delayed provision is captured unanswered and delivered later.
    """

    def __init__(self, provisioner: str) -> None:
        self.provisioner = provisioner
        self.clock = [1000.0]
        self.records: dict[str, list[str]] = {}
        self.pre_intent_failures = 0
        self.proof_transport_failures = 0
        self.delay_next_provision = False
        self.deliver_before_finalize = False
        self.delayed: dict | None = None
        self.delayed_outcome: object = None
        self.tenant_drops: list[str] = []

    @staticmethod
    def _refused(path: str, status: int = 403) -> postgresql_client.PostgreSQLServiceError:
        return postgresql_client.PostgreSQLServiceError(f"postgresql-service {path} failed with status {status}")

    def sleep(self, seconds: float) -> None:
        self.clock[0] += seconds

    def deliver(self) -> None:
        payload, self.delayed = self.delayed, None
        try:
            self.delayed_outcome = self._provision("/v1/teams/provision", payload)
        except postgresql_client.PostgreSQLServiceError as exc:
            self.delayed_outcome = exc

    def _fenced(self, path: str, payload: dict) -> None:
        if self.clock[0] <= payload["not_after"]:
            raise self._refused(path, 409)

    def _provision(self, path: str, payload: dict) -> dict:
        if self.clock[0] > payload["not_after"]:
            raise self._refused(path, 409)
        record = self.records.get(payload["team_id"])
        if record is not None and record[1] == "retired":
            raise self._refused(path)
        self.records[payload["team_id"]] = [payload["principal_token"], "active"]
        return {"created": True}

    def __call__(self, path: str, payload: dict, bearer: str) -> dict:
        team_id = payload["team_id"]
        record = self.records.get(team_id)
        if bearer != self.provisioner:
            if path != "/v1/teams/drop" or record is None or record[0] != bearer:
                raise self._refused(path)
            record[1] = "retired"
            self.tenant_drops.append(team_id)
            return {"dropped": [f"proj_team_{team_id}"]}
        if path == "/v1/teams/provision":
            if self.delay_next_provision:
                self.delay_next_provision = False
                self.delayed = payload
                raise TimeoutError("timed out")
            if self.pre_intent_failures:
                # The request failed before the Service recorded any intent or ran any DDL.
                self.pre_intent_failures -= 1
                raise self._refused(path, 502)
            return self._provision(path, payload)
        if path == "/v1/teams/drop":
            if self.proof_transport_failures:
                self.proof_transport_failures -= 1
                raise OSError("connection reset")
            self._fenced(path, payload)
            if record is not None:
                raise self._refused(path)
            return {"dropped": []}
        if self.deliver_before_finalize:
            self.deliver_before_finalize = False
            self.deliver()
            record = self.records.get(team_id)
        self._fenced(path, payload)
        if record is not None and record[1] != "retired":
            raise self._refused(path)
        self.records.pop(team_id, None)
        return {"finalized": True}


def _lease(**changes):
    values = {
        "team_id": TEAM_ID,
        "container_id": RUNTIME_ID,
        "owner": OWNER,
        "principal": ("account", OWNER),
        "cleanup_nonce": "",
    }
    values.update(changes)
    return resources._AuthorizationLease(**values)


class HostedTeamOperationEdgeTests(unittest.TestCase):
    def test_create_validates_name_inference_and_pending_cleanup_ownership(self) -> None:
        with (
            mock.patch.object(resources, "_validated_team_name", side_effect=ValueError("name")),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._create(TEAM_ID, {}, OWNER)
        with (
            mock.patch.object(
                lifecycle.inference_config,
                "normalize",
                side_effect=lifecycle.inference_config.InferenceConfigError("model"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._create(TEAM_ID, {}, OWNER)

        for cleanup_owner in ("other", OWNER):
            pending = SimpleNamespace(owner=cleanup_owner)
            with (
                mock.patch.object(resources, "_cleanup_record", return_value=pending),
                self.assertRaises(state.ApiError),
            ):
                lifecycle._create(TEAM_ID, {}, OWNER)

    def test_idempotent_create_preserves_owner_name_and_updates_inference(self) -> None:
        existing = _container()
        inference = SimpleNamespace(provider="openai", model="model", effort="low")
        with (
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            mock.patch.object(resources, "_get_container", return_value=_container(labels={"team.owner": "other"})),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._create(TEAM_ID, {}, OWNER)

        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=inference),
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            mock.patch.object(resources, "_get_container", return_value=existing),
            mock.patch.object(resources, "_require_team_runtime"),
            mock.patch.object(resources, "_require_team_isolation"),
            mock.patch.object(resources, "_team_name_from_anchor", return_value="Persisted"),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._create(TEAM_ID, {"team_name": "Changed"}, OWNER)

        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=inference),
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            mock.patch.object(resources, "_get_container", return_value=existing),
            mock.patch.object(resources, "_require_team_runtime"),
            mock.patch.object(resources, "_require_team_isolation"),
            mock.patch.object(resources, "_team_name_from_anchor", return_value="Persisted"),
            mock.patch.object(state._inference_store, "save") as save,
        ):
            result = lifecycle._create(TEAM_ID, {}, OWNER)
        self.assertFalse(result["created"])
        self.assertEqual(result["team_name"], "Persisted")
        save.assert_called_once_with(TEAM_ID, inference)

    def test_new_create_requires_clean_storage_and_commits_all_resources(self) -> None:
        inference = SimpleNamespace(provider="openai", model="model", effort="low")
        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=inference),
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            mock.patch.object(resources, "_get_container", return_value=None),
            mock.patch.object(lifecycle, "_teardown_storage", return_value=False),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._create(TEAM_ID, {}, OWNER)

        container = _container()
        network = object()
        state._docker.containers.create = mock.Mock(return_value=container)
        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=inference),
            mock.patch.object(resources, "_cleanup_record", return_value=None),
            mock.patch.object(resources, "_get_container", return_value=None),
            mock.patch.object(lifecycle, "_teardown_storage", return_value=True),
            mock.patch.object(resources, "_reserve_capacity", return_value=contextlib.nullcontext()),
            mock.patch.object(resources, "_require_team_runtime"),
            mock.patch.object(lifecycle.postgresql_service_client, "provision_team"),
            mock.patch.object(resources, "_ensure_team_network", return_value=network),
            mock.patch.object(resources, "_wire_network_deps"),
            mock.patch.object(resources, "_require_network_policy"),
            mock.patch.object(lifecycle.container_spec, "build_team_kwargs", return_value={"name": "team"}),
            mock.patch.object(resources, "_start_team_with_isolation"),
            mock.patch.object(state._inference_store, "save"),
        ):
            result = lifecycle._create(TEAM_ID, {"team_name": "Team"}, OWNER)
        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "running")

    def test_create_rollback_distinguishes_incomplete_api_and_generic_failures(self) -> None:
        inference = SimpleNamespace(provider="openai", model="model", effort="low")

        def run(error: Exception, cleanup_complete: bool) -> state.ApiError:
            patches = (
                mock.patch.object(lifecycle.inference_config, "normalize", return_value=inference),
                mock.patch.object(resources, "_cleanup_record", return_value=None),
                mock.patch.object(resources, "_get_container", return_value=None),
                mock.patch.object(lifecycle, "_teardown_storage", return_value=True),
                mock.patch.object(resources, "_reserve_capacity", return_value=contextlib.nullcontext()),
                mock.patch.object(resources, "_require_team_runtime"),
                mock.patch.object(lifecycle.postgresql_service_client, "provision_team", side_effect=error),
                mock.patch.object(
                    lifecycle,
                    "_teardown",
                    return_value=resources._CleanupResult(cleanup_complete, cleanup_complete),
                ),
            )
            with contextlib.ExitStack() as stack:
                for current in patches:
                    stack.enter_context(current)
                with self.assertRaises(state.ApiError) as caught:
                    lifecycle._create(TEAM_ID, {}, OWNER)
            return caught.exception

        self.assertIn("rollback is incomplete", run(RuntimeError("failed"), False).message)
        api_error = state.ApiError(409, "contract")
        self.assertIs(run(api_error, True), api_error)
        self.assertIn("rolled back", run(RuntimeError("failed"), True).message)

    def _database_lifecycle(self, stack: contextlib.ExitStack) -> _PostgreSQLService:
        """Run real create, rollback, destroy, cleanup records, and the Team client against the Service authority."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        provisioner = root / "provisioner"
        provisioner.write_text("b" * 64, encoding="utf-8")
        service = _PostgreSQLService("b" * 64)
        state._docker.containers.create = mock.Mock(return_value=_container())
        succeed = mock.Mock(return_value=True)
        patches = (
            mock.patch.object(lifecycle.cleanup_state, "STATE_DIR", root / "cleanup"),
            mock.patch.object(lifecycle, "postgresql_service_client", postgresql_client),
            mock.patch.object(postgresql_client, "PRINCIPAL_DIR", root / "principals"),
            mock.patch.object(postgresql_client, "PROVISIONER_TOKEN_FILE", provisioner),
            mock.patch.object(postgresql_client, "_call", side_effect=service),
            mock.patch.object(postgresql_client.time, "time", side_effect=lambda: service.clock[0]),
            mock.patch.object(postgresql_client.time, "sleep", side_effect=service.sleep),
            mock.patch.object(
                lifecycle.inference_config,
                "normalize",
                return_value=SimpleNamespace(provider="openai", model="model", effort="low"),
            ),
            mock.patch.object(resources, "_get_container", return_value=None),
            mock.patch.object(resources, "_reserve_capacity", return_value=contextlib.nullcontext()),
            mock.patch.object(resources, "_require_team_runtime"),
            mock.patch.object(resources, "_ensure_team_network"),
            mock.patch.object(resources, "_wire_network_deps"),
            mock.patch.object(resources, "_require_network_policy"),
            mock.patch.object(lifecycle.container_spec, "build_team_kwargs", return_value={"name": "team"}),
            mock.patch.object(resources, "_start_team_with_isolation"),
            mock.patch.object(state._inference_store, "save"),
            mock.patch.object(state, "_clear_team_id_runtime_state"),
            mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
            mock.patch.object(lifecycle, "_owned_teardown_runtime", return_value=(True, None)),
            mock.patch.multiple(
                lifecycle,
                _stop_teardown_runtime=succeed,
                _teardown_preparation_helpers=succeed,
                _teardown_assistants=succeed,
                _teardown_storage=succeed,
                _teardown_inference=succeed,
                _teardown_assistant_integrations=succeed,
                _teardown_assistant_stored_inputs=succeed,
                _teardown_network_planes=succeed,
                _remove_teardown_runtime=succeed,
                _teardown_volumes=succeed,
            ),
        )
        for current in patches:
            stack.enter_context(current)
        return service

    def test_pre_intent_provisioning_failure_rolls_back_retries_and_destroys(self) -> None:
        with contextlib.ExitStack() as stack:
            service = self._database_lifecycle(stack)
            principal = postgresql_client._principal_path(TEAM_ID)

            # Provisioning fails before the Service records an intent, and the absence proof is first unreachable.
            service.pre_intent_failures = 1
            service.proof_transport_failures = 1
            with self.assertRaisesRegex(state.ApiError, "rollback is incomplete"):
                lifecycle._create(TEAM_ID, {}, OWNER)
            record = lifecycle.cleanup_state.load(TEAM_ID)
            self.assertIsNotNone(record)
            self.assertTrue(principal.exists())
            with self.assertRaisesRegex(state.ApiError, "incomplete teardown") as conflict:
                lifecycle._create(TEAM_ID, {}, OWNER)
            self.assertEqual(conflict.exception.status, HTTPStatus.CONFLICT)

            # The retried destroy clears the cleanup record only through the Service's absence proof.
            destroyed = lifecycle._destroy(TEAM_ID, _lease(container_id="", cleanup_nonce=record.nonce))
            self.assertTrue(destroyed["destroyed"])
            self.assertIsNone(lifecycle.cleanup_state.load(TEAM_ID))
            self.assertFalse(principal.exists())
            self.assertEqual((service.records, service.tenant_drops), ({}, []))
            # An answered provision never delays cleanup.
            self.assertEqual(service.clock[0], 1000.0)

            # A clean rollback leaves nothing behind, so create can succeed and a real destroy still uses the tenant.
            service.pre_intent_failures = 1
            with self.assertRaisesRegex(state.ApiError, "was rolled back"):
                lifecycle._create(TEAM_ID, {}, OWNER)
            self.assertIsNone(lifecycle.cleanup_state.load(TEAM_ID))
            self.assertTrue(lifecycle._create(TEAM_ID, {}, OWNER)["created"])
            self.assertEqual(service.records[TEAM_ID], [principal.read_text(encoding="ascii"), "active"])
            cleanup = lifecycle._teardown(TEAM_ID, owner=OWNER, runtime_id=RUNTIME_ID)
            self.assertTrue(cleanup.complete)
            self.assertEqual((service.records, service.tenant_drops), ({}, [TEAM_ID]))
            self.assertIsNone(lifecycle.cleanup_state.load(TEAM_ID))

    def test_a_delayed_provision_never_lands_after_rollback_proves_absence(self) -> None:
        for window in ("before finalization", "after finalization"):
            with self.subTest(window=window), contextlib.ExitStack() as stack:
                service = self._database_lifecycle(stack)
                service.delay_next_provision = True
                service.deliver_before_finalize = window == "before finalization"
                with self.assertRaisesRegex(state.ApiError, "was rolled back"):
                    lifecycle._create(TEAM_ID, {}, OWNER)
                if service.delayed is not None:
                    service.deliver()

                # Rollback waited out the unanswered request's fence before proving absence, so it was refused.
                self.assertGreater(service.clock[0], 1000.0 + postgresql_client.SERVICE_TIMEOUT_SECONDS)
                self.assertIn("status 409", str(service.delayed_outcome))
                self.assertEqual((service.records, service.tenant_drops), ({}, []))
                self.assertIsNone(lifecycle.cleanup_state.load(TEAM_ID))
                self.assertEqual(list(postgresql_client.PRINCIPAL_DIR.iterdir()), [])
                self.assertTrue(lifecycle._create(TEAM_ID, {}, OWNER)["created"])

    def test_generation_state_deletion_contains_brain_and_journal_failures(self) -> None:
        self.assertEqual(
            lifecycle._delete_generation_state(TEAM_ID, ""),
            {"brain_checkpoints", "action_checkpoints"},
        )
        with (
            mock.patch.object(
                state._brain_runtime,
                "delete_thread",
                side_effect=lifecycle.brain_runtime_client.BrainRuntimeError("brain"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._delete_generation_state(TEAM_ID, RUNTIME_ID)

        journal = mock.Mock()
        journal.purge.side_effect = lifecycle.action_journal.ActionJournalError("journal")
        with (
            mock.patch.object(state._brain_runtime, "delete_thread"),
            mock.patch.object(state, "_action_execution_journal", return_value=journal),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._delete_generation_state(TEAM_ID, RUNTIME_ID)
        journal.purge.side_effect = None
        with (
            mock.patch.object(state._brain_runtime, "delete_thread"),
            mock.patch.object(state, "_action_execution_journal", return_value=journal),
        ):
            self.assertEqual(
                lifecycle._delete_generation_state(TEAM_ID, RUNTIME_ID),
                {"brain_checkpoints", "action_checkpoints"},
            )

    def test_destroy_revalidates_cleanup_anchor_and_stops_running_runtime(self) -> None:
        container = _container()
        lock = mock.Mock()
        lock.acquire.return_value = False
        with (
            mock.patch.object(resources, "_require_current_authorization", return_value=container),
            mock.patch.object(lifecycle.cleanup_state, "begin"),
            mock.patch.object(resources, "_fail_stop_team") as stop,
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._destroy(TEAM_ID, _lease())
        stop.assert_called_once_with(container, timeout=30)

        stopped_container = _container(status="stopped")
        lock.acquire.return_value = False
        with (
            mock.patch.object(resources, "_require_current_authorization", return_value=stopped_container),
            mock.patch.object(lifecycle.cleanup_state, "begin"),
            mock.patch.object(resources, "_fail_stop_team") as stop,
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._destroy(TEAM_ID, _lease())
        stop.assert_not_called()

        cleanup_lease = _lease(cleanup_nonce="nonce")
        lock.acquire.return_value = False
        with (
            mock.patch.object(resources, "_require_cleanup_authorization") as require,
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._destroy(TEAM_ID, cleanup_lease)
        require.assert_called_once_with(TEAM_ID, cleanup_lease)

        with (
            mock.patch.object(resources, "_require_current_authorization", return_value=container),
            mock.patch.object(
                lifecycle.cleanup_state,
                "begin",
                side_effect=lifecycle.cleanup_state.CleanupStateError("state"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._destroy(TEAM_ID, _lease())

    def test_destroy_requires_complete_teardown_and_exact_residue_proof(self) -> None:
        lock = mock.Mock()
        lock.acquire.return_value = True
        complete = resources._CleanupResult(True, True, tuple(lifecycle._TEAM_RESIDUE_ABSENCE))
        base = (
            mock.patch.object(resources, "_require_cleanup_authorization"),
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            mock.patch.object(lifecycle, "_delete_generation_state", return_value=set()),
            mock.patch.object(state, "_clear_team_id_runtime_state"),
        )
        for cleanup in (
            resources._CleanupResult(False, False),
            resources._CleanupResult(True, True, ("database",)),
            complete,
        ):
            with contextlib.ExitStack() as stack:
                for current in base:
                    stack.enter_context(current)
                stack.enter_context(mock.patch.object(lifecycle, "_teardown", return_value=cleanup))
                if cleanup is complete:
                    result = lifecycle._destroy(TEAM_ID, _lease(cleanup_nonce="nonce"))
                    self.assertTrue(result["destroyed"])
                else:
                    with self.assertRaises(state.ApiError):
                        lifecycle._destroy(TEAM_ID, _lease(cleanup_nonce="nonce"))
        self.assertEqual(lock.release.call_count, 3)

    def test_destroy_ends_only_this_teams_expired_human_continuations(self) -> None:
        def expired(team_id: str, generation: str) -> object:
            pending = harness.hosted_assistants._PendingHostedChat(object(), (), (), OWNER, (generation,))
            return state.action_challenges.PendingHumanChallenge("e" * 32, team_id, 0.0, SimpleNamespace(), pending)

        humans = state.action_challenges.HumanChallengeStore(retain_expired=True)
        foreign = expired("team_2", "d" * 64)
        humans._expired.extend((expired(TEAM_ID, "c" * 64), foreign))
        journal = mock.Mock()
        lock = mock.Mock()
        lock.acquire.return_value = True
        complete = resources._CleanupResult(True, True, tuple(lifecycle._TEAM_RESIDUE_ABSENCE))
        with (
            mock.patch.object(state, "_human_challenges", humans),
            mock.patch.object(state, "_action_execution_journal", return_value=journal),
            mock.patch.object(resources, "_require_cleanup_authorization"),
            mock.patch.object(state, "_chat_lock_for", return_value=lock),
            mock.patch.object(lifecycle, "_delete_generation_state", return_value=set()),
            mock.patch.object(lifecycle, "_teardown", return_value=complete),
            mock.patch.object(state, "_clear_team_id_runtime_state"),
        ):
            self.assertTrue(lifecycle._destroy(TEAM_ID, _lease(cleanup_nonce="nonce"))["destroyed"])

        journal.purge.assert_called_once_with("c" * 64)
        self.assertEqual(humans.drain_expired(), (foreign,))

    def test_list_status_inference_logs_and_lifecycle_operations_map_failures(self) -> None:
        own = _container()
        foreign = _container(id="b", labels={"team.owner": "other"})
        state._docker.containers.list = mock.Mock(return_value=[own, foreign])
        with mock.patch.object(resources, "_describe", side_effect=lambda item: {"id": item.id}):
            self.assertEqual(len(lifecycle._list(owner=None)["teams"]), 2)
            self.assertEqual(lifecycle._list(owner=OWNER)["teams"], [{"id": own.id}])

        lease = _lease()
        with (
            mock.patch.object(resources, "_require_current_authorization", return_value=own),
            mock.patch.object(resources, "_describe", return_value={"status": "running"}),
        ):
            self.assertEqual(lifecycle._status(TEAM_ID, lease)["status"], "running")

        with (
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(
                state._inference_store,
                "load",
                side_effect=lifecycle.inference_config.InferenceConfigError("missing"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._inference_status(TEAM_ID, lease)
        for error, status in (
            (lifecycle.inference_config.InferenceConfigMissingError("missing"), HTTPStatus.CONFLICT),
            (lifecycle.inference_config.InferenceConfigError("invalid"), HTTPStatus.SERVICE_UNAVAILABLE),
        ):
            with (
                mock.patch.object(resources, "_require_current_authorization"),
                mock.patch.object(state._inference_store, "load", side_effect=error),
                self.assertRaises(state.ApiError) as caught,
            ):
                lifecycle._inference_status(TEAM_ID, lease)
            self.assertEqual(caught.exception.status, status)
        config = SimpleNamespace(provider="openai", model="model", effort="low")
        with (
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(state._inference_store, "load", return_value=config),
        ):
            self.assertEqual(lifecycle._inference_status(TEAM_ID, lease)["model"], "model")

        for body in (
            None,
            {},
            {"provider": "openai", "model": "m"},
            {"provider": "openai", "model": "m", "effort": 1},
            {"provider": "openai", "model": "m", "effort": "low", "extra": True},
        ):
            with self.assertRaises(state.ApiError):
                lifecycle._configure_inference(TEAM_ID, body, lease)
        with (
            mock.patch.object(
                lifecycle.inference_config,
                "normalize",
                side_effect=lifecycle.inference_config.InferenceConfigError("invalid"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._configure_inference(TEAM_ID, {"provider": "openai", "model": "m", "effort": "low"}, lease)
        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=config),
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
            mock.patch.object(
                state._inference_store,
                "save",
                side_effect=lifecycle.inference_config.InferenceConfigError("save"),
            ),
            self.assertRaises(state.ApiError),
        ):
            lifecycle._configure_inference(TEAM_ID, {"provider": "openai", "model": "m", "effort": "low"}, lease)
        with (
            mock.patch.object(lifecycle.inference_config, "normalize", return_value=config),
            mock.patch.object(resources, "_require_current_authorization"),
            mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
            mock.patch.object(state._inference_store, "save"),
        ):
            self.assertEqual(
                lifecycle._configure_inference(TEAM_ID, {"provider": "openai", "model": "m", "effort": "low"}, lease)[
                    "model"
                ],
                "model",
            )

        with mock.patch.object(resources, "_require_current_authorization", return_value=own):
            self.assertEqual(lifecycle._logs(TEAM_ID, 10, lease)["logs"], "logs")

    def test_runtime_lifecycle_stops_and_starts_only_when_required(self) -> None:
        lease = _lease()
        for op, status, stop_count, start_count in (
            ("stop", "running", 1, 0),
            ("start", "stopped", 0, 1),
            ("restart", "running", 1, 1),
        ):
            container = _container(status=status)
            container.reload = mock.Mock(side_effect=lambda: None)
            if op == "restart":
                statuses = iter(("running", "stopped"))
                container.reload = mock.Mock(
                    side_effect=lambda current=container, values=statuses: setattr(current, "status", next(values))
                )
            with (
                mock.patch.object(resources, "_require_current_authorization", return_value=container),
                mock.patch.object(lifecycle.hosted_chat_lifecycle, "cancel_replayable_human"),
                mock.patch.object(resources, "_require_team_runtime"),
                mock.patch.object(resources, "_fail_stop_team") as stop,
                mock.patch.object(resources, "_start_team_with_isolation") as start,
            ):
                lifecycle._lifecycle(TEAM_ID, op, lease)
            self.assertEqual(stop.call_count, stop_count)
            self.assertEqual(start.call_count, start_count)


if __name__ == "__main__":
    unittest.main()
