"""Local Team display names (ADR-0088): the name store, rename, create, and confirmed deletion."""

from __future__ import annotations

import json
import tempfile
import threading
import types
import unittest
from http import HTTPStatus
from pathlib import Path
from unittest import mock

from local import lifecycle as local_lifecycle
from local import names as local_names
from local.errors import ApiProblemError as ApiProblem
from local.labels import TEAM_LABEL, TEAM_NAME_LABEL

NETWORK_A = "a" * 64
NETWORK_B = "b" * 64


class TeamNameStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "inference"
        self.store = local_names.TeamNameStore(self.root)

    def unavailable(self, action) -> None:
        with self.assertRaises(ApiProblem) as caught:
            action()
        self.assertEqual((caught.exception.status, caught.exception.code), (503, "team-names-unavailable"))

    def test_a_record_is_the_name_of_its_own_incarnation_and_absence_falls_back(self) -> None:
        self.assertIsNone(self.store.load("team_1", NETWORK_A))
        self.store.save("team_1", NETWORK_A, "Growth")
        self.assertEqual(self.store.load("team_1", NETWORK_A), "Growth")
        self.assertEqual(self.store._path("team_1").stat().st_mode & 0o777, 0o600)
        # A record of another network incarnation is never a current name.
        self.unavailable(lambda: self.store.load("team_1", NETWORK_B))

    def test_an_unsafe_or_foreign_record_fails_closed(self) -> None:
        path = self.store._path("team_1")
        base = {"schema": 1, "team_id": "team_1", "network_id": NETWORK_A, "team_name": "Growth"}
        for raw in (
            b"not json",
            b"[]",
            json.dumps({**base, "extra": 1}).encode(),
            json.dumps({**base, "schema": 2}).encode(),
            json.dumps({**base, "team_id": "team_2"}).encode(),
            json.dumps({**base, "team_name": "Équipe"}).encode(),
            json.dumps({**base, "team_name": "x" * 5000}).encode(),
            json.dumps({**base, "team_name": "Growth\u200b"}).encode(),
            json.dumps({**base, "schema": True}).encode(),
            json.dumps({**base, "schema": 1.0}).encode(),
            b'{"schema":1,"schema":1,"team_id":"team_1","network_id":"'
            + NETWORK_A.encode()
            + b'","team_name":"Growth"}',
        ):
            with self.subTest(raw=raw[:40]):
                self.root.mkdir(exist_ok=True)
                path.write_bytes(raw)
                self.unavailable(lambda: self.store.load("team_1", NETWORK_A))
        path.unlink()
        path.symlink_to(self.root / "missing")
        self.unavailable(lambda: self.store.load("team_1", NETWORK_A))
        path.unlink()
        path.mkdir()
        self.unavailable(lambda: self.store.load("team_1", NETWORK_A))
        # A path that is a directory also cannot be removed as a record.
        self.unavailable(lambda: self.store.delete("team_1"))

    def test_writes_and_listing_fail_closed(self) -> None:
        self.root.parent.mkdir(exist_ok=True)
        self.root.write_bytes(b"")
        self.unavailable(lambda: self.store.save("team_1", NETWORK_A, "Growth"))
        self.unavailable(self.store.delete_all)

    def test_delete_removes_only_the_teams_own_files_and_delete_all_every_owned_one(self) -> None:
        self.store.delete("team_1")
        self.store.delete_all()
        self.store.save("team_1", NETWORK_A, "Growth")
        self.store.save("team_2", NETWORK_B, "Sales")
        own_temporary = self.root / f".{self.store._path('team_1').name}.{'0' * 16}.tmp"
        other_temporary = self.root / f".{self.store._path('team_2').name}.{'1' * 16}.tmp"
        unrelated = self.root / "unrelated.json"
        for path in (own_temporary, other_temporary, unrelated):
            path.write_bytes(b"{}")
        self.store.delete("team_1")
        self.assertFalse(self.store._path("team_1").exists())
        self.assertFalse(own_temporary.exists())
        self.assertTrue(other_temporary.exists())
        self.store.delete_all()
        self.assertEqual(sorted(path.name for path in self.root.iterdir()), ["unrelated.json"])


def _network(team_id: str, network_id: str, label: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(id=network_id, attrs={"Labels": {TEAM_LABEL: team_id, TEAM_NAME_LABEL: label}})


class NamedTeamCase(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.networks = {"team_a": _network("team_a", NETWORK_A, "Marketing")}
        locks: dict[str, threading.RLock] = {}
        lifecycle = types.SimpleNamespace(
            _managed_team_networks=lambda: list(self.networks.values()),
            _network=self.lookup,
            _validate_network=lambda network, _team_id, **_kwargs: network.attrs["Labels"][TEAM_NAME_LABEL],
            _base_labels=lambda team_id, _kind: {TEAM_LABEL: team_id},
            _network_name=lambda team_id: f"shimpz-{team_id}",
        )
        self.controller = types.SimpleNamespace(
            _names_lock=threading.RLock(),
            _lock=lambda team_id: locks.setdefault(team_id, threading.RLock()),
            assistant_lifecycle=lifecycle,
            team_names=local_names.TeamNameStore(Path(directory.name) / "inference"),
            storage=types.SimpleNamespace(destroy=mock.Mock()),
            inference_store=types.SimpleNamespace(delete=mock.Mock()),
            client=types.SimpleNamespace(networks=types.SimpleNamespace(create=self.create_network)),
        )

    def lookup(self, team_id: str, *, required: bool = True):
        network = self.networks.get(team_id)
        if network is None and required:
            raise ApiProblem(HTTPStatus.NOT_FOUND, "Team not found", code="team-not-found")
        return network

    def create_network(self, _name: str, **kwargs):
        labels = kwargs["labels"]
        network = _network(labels[TEAM_LABEL], labels[TEAM_LABEL][-1] * 64, labels[TEAM_NAME_LABEL])
        self.networks[labels[TEAM_LABEL]] = network
        return network

    def call(self, function, *args):
        return function(self.controller, *args)

    def problem(self, function, *args) -> str:
        with self.assertRaises(ApiProblem) as caught:
            self.call(function, *args)
        return caught.exception.code


class RenameTests(NamedTeamCase):
    def test_a_rename_changes_only_the_display_name_and_returning_to_the_label_drops_the_record(self) -> None:
        store = self.controller.team_names
        renamed = self.call(local_names.rename_team, "team_a", "Growth")
        self.assertEqual(renamed, {"team_id": "team_a", "team_name": "Growth"})
        self.assertEqual(store.load("team_a", NETWORK_A), "Growth")
        self.assertEqual(
            self.call(local_names.list_teams),
            {"teams": [{"team_id": "team_a", "team_name": "Growth", "status": "running"}]},
        )
        # The same name again is a no-op that still revalidates the stored name.
        self.assertEqual(self.call(local_names.rename_team, "team_a", "Growth")["team_name"], "Growth")
        self.call(local_names.rename_team, "team_a", "Marketing")
        self.assertIsNone(store.load("team_a", NETWORK_A))

    def test_names_are_unique_ignoring_case_and_malformed_names_or_missing_teams_are_refused(self) -> None:
        self.networks["team_b"] = _network("team_b", NETWORK_B, "Sales")
        self.assertEqual(self.problem(local_names.rename_team, "team_b", "MARKETING"), "team-name-taken")
        self.assertEqual(self.problem(local_names.rename_team, "team_b", "Équipe"), "invalid-team-name")
        self.assertEqual(self.problem(local_names.rename_team, "team_c", "Other"), "team-not-found")
        self.networks["team_b"].attrs["Labels"][TEAM_LABEL] = 7
        self.assertEqual(self.problem(local_names.list_teams), "ownership-conflict")

    def test_concurrent_renames_to_one_name_admit_exactly_one(self) -> None:
        self.networks["team_b"] = _network("team_b", NETWORK_B, "Sales")
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def rename(team_id: str) -> None:
            barrier.wait()
            try:
                self.call(local_names.rename_team, team_id, "Shared")
                outcomes.append("renamed")
            except ApiProblem as exc:
                outcomes.append(exc.code)

        threads = [threading.Thread(target=rename, args=(team_id,)) for team_id in ("team_a", "team_b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["renamed", "team-name-taken"])


class CreateTests(NamedTeamCase):
    def test_create_compares_the_current_name_and_refuses_a_name_another_team_has(self) -> None:
        self.call(local_names.rename_team, "team_a", "Growth")
        # The creation label no longer creates this id: its current name differs.
        self.assertEqual(self.problem(local_names.create_team, "team_a", "Marketing"), "team-name-conflict")
        self.assertFalse(self.call(local_names.create_team, "team_a", "Growth")["created"])
        self.assertEqual(self.problem(local_names.create_team, "team_g", "growth"), "team-name-taken")

    def test_a_new_incarnation_starts_without_a_leftover_record(self) -> None:
        self.controller.team_names.save("team_c", NETWORK_A, "Stale")
        created = self.call(local_names.create_team, "team_c", "Research")
        self.assertTrue(created["created"])
        self.assertIsNone(self.controller.team_names.load("team_c", "c" * 64))
        self.assertEqual(self.controller.team_names.load("team_c", NETWORK_A), None)


class ConfirmedDeletionTests(NamedTeamCase):
    def test_a_stale_name_deletes_nothing(self) -> None:
        self.call(local_names.rename_team, "team_a", "Growth")
        self.controller.chat_turn_service = mock.Mock()
        self.assertEqual(self.problem(local_lifecycle.destroy_team, "team_a", "Marketing"), "team-name-mismatch")
        self.controller.chat_turn_service.assert_not_called()
        self.assertEqual(self.controller.chat_turn_service.mock_calls, [])
        self.assertEqual(self.controller.team_names.load("team_a", NETWORK_A), "Growth")


if __name__ == "__main__":
    unittest.main()
