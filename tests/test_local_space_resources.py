"""The Space's read-only resource signal: running services, memory, and its storage filesystem."""

import os
import unittest
from email.message import Message
from http import HTTPStatus
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from docker.errors import APIError

from local import authority, lifecycle
from local.errors import ApiProblemError
from local.http import audit as http_audit
from local.http import server
from protocol.http.v1 import resources as contract

SPACE_ID = "a" * 32
GIB = 2**30
ASSISTANT_LABELS = [
    "com.shimpz.local.managed=1",
    "com.shimpz.local.profile=local-v1",
    f"com.shimpz.local.space-id={SPACE_ID}",
    "com.shimpz.local.kind=assistant",
]


def _container(name: str, container_id: str) -> dict[str, object]:
    return {"Id": container_id, "Names": [f"/{name}"]}


def _stats(usage: int, **details: int) -> dict[str, object]:
    return {"memory_stats": {"usage": usage, "stats": details}}


class FakeApi:
    """Docker's Engine API as the measurement calls it: each path answers its value, or raises an exception."""

    def __init__(self, answers: dict[str, object]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, object, float]] = []

    def _url(self, path: str, *args: str) -> str:
        return path.format(*args)

    def _get(self, url: str, *, params: object, timeout: float) -> object:
        self.calls.append((url, params, timeout))
        answer = self.answers[url]
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def _result(self, response: object, *, json: bool) -> object:
        return response


class ListingApi(FakeApi):
    """A listing answers by its label filter: the Compose project's members, or the Space's owned Assistants."""

    def __init__(self, members: object, assistants: object, answers: dict[str, object]) -> None:
        super().__init__(answers)
        self.members = members
        self.assistants = assistants

    def _get(self, url: str, *, params: object, timeout: float) -> object:
        if url != "/containers/json":
            return super()._get(url, params=params, timeout=timeout)
        self.calls.append((url, params, timeout))
        answer = self.assistants if "com.shimpz.local.kind=assistant" in params["filters"] else self.members
        if isinstance(answer, BaseException):
            raise answer
        return answer


def _controller(api: FakeApi, root: Path = Path("/var/lib/shimpz-local/storage")) -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(api=api), space_id=SPACE_ID, storage=SimpleNamespace(root=root))


def _statvfs(blocks: int, free: int, size: int = 4096) -> os.statvfs_result:
    return os.statvfs_result((size, size, blocks, free, free, 0, 0, 0, 0, 255))


class SpaceResourceTests(unittest.TestCase):
    def measure(self, api: FakeApi, statvfs: object = None) -> dict[str, object]:
        statvfs = statvfs or mock.Mock(return_value=_statvfs(16 * GIB // 4096, 12 * GIB // 4096))
        with mock.patch.object(lifecycle.os, "statvfs", statvfs):
            measured = lifecycle.space_resources(_controller(api))
        self.assertEqual(contract.canonical_space_resources(measured), measured)
        return measured

    def healthy(self, **answers: object) -> ListingApi:
        members = [_container(name, f"id-{name}") for name in sorted(lifecycle.SERVICES)]
        answers = {
            "/info": {"MemTotal": 4 * GIB},
            **{f"/containers/id-{name}/stats": _stats(GIB // 16) for name in lifecycle.SERVICES},
            "/containers/id-assistant/stats": _stats(GIB // 4, inactive_file=GIB // 8),
            **answers,
        }
        return ListingApi(members, [_container("shimpz-team_1-helper", "id-assistant")], answers)

    def test_measures_every_running_service_owned_assistant_and_the_storage_filesystem(self) -> None:
        api = self.healthy()
        statvfs = mock.Mock(return_value=_statvfs(16 * GIB // 4096, 12 * GIB // 4096))

        measured = self.measure(api, statvfs)

        self.assertEqual(
            measured,
            {
                "services_running": 7,
                "services_total": 7,
                "memory_used_bytes": 7 * (GIB // 16) + GIB // 8,
                "memory_total_bytes": 4 * GIB,
                "storage_used_bytes": 4 * GIB,
                "storage_total_bytes": 16 * GIB,
            },
        )
        statvfs.assert_called_once_with(Path("/var/lib/shimpz-local/storage"))
        listings = [params["filters"] for url, params, _timeout in api.calls if url == "/containers/json"]
        self.assertEqual(
            listings,
            [
                '{"label": ["com.docker.compose.project=shimpz-space"], "status": ["running"]}',
                '{"label": ' + str(ASSISTANT_LABELS).replace("'", '"') + ', "status": ["running"]}',
            ],
        )
        stats = [params for url, params, _timeout in api.calls if url.endswith("/stats")]
        self.assertEqual(stats, [{"stream": "0", "one-shot": "1"}] * 8)
        self.assertTrue(all(0 < timeout <= lifecycle.BUDGET_SECONDS for _url, _params, timeout in api.calls))

    def test_counts_a_missing_or_stopped_service_and_ignores_foreign_containers(self) -> None:
        members = [
            _container("shimpz-team", "id-team"),
            _container("shimpz-admin", "id-admin"),
            _container("shimpz-account-egress-init", "id-init"),
            _container("shimpz-store", "id-store"),
            {"Id": "id-unnamed", "Names": []},
            {"Id": "id-odd", "Names": [7]},
        ]
        api = ListingApi(
            members,
            [],
            {
                "/info": {"MemTotal": 4 * GIB},
                "/containers/id-team/stats": _stats(GIB // 4, total_inactive_file=GIB // 8),
                "/containers/id-admin/stats": _stats(GIB // 8, inactive_file=GIB // 4),
            },
        )

        measured = self.measure(api)

        self.assertEqual((measured["services_running"], measured["services_total"]), (2, 7))
        self.assertEqual(measured["memory_used_bytes"], GIB // 8 + GIB // 8)

    def test_an_incomplete_memory_measurement_is_null_never_zero(self) -> None:
        cases = {
            "stats refused": {"/containers/id-shimpz-brain/stats": APIError("gone")},
            "transport failure": {"/containers/id-shimpz-brain/stats": OSError("reset")},
            "usage missing": {"/containers/id-shimpz-brain/stats": {"memory_stats": {}}},
            "stats not an object": {"/containers/id-shimpz-brain/stats": []},
            "usage not a count": {"/containers/id-shimpz-brain/stats": {"memory_stats": {"usage": True}}},
            "memory total missing": {"/info": {}},
            "memory total zero": {"/info": {"MemTotal": 0}},
            "info not an object": {"/info": "4 GiB"},
            "info unreadable": {"/info": ValueError("not JSON")},
        }
        for name, answers in cases.items():
            with self.subTest(name):
                measured = self.measure(self.healthy(**answers))
                self.assertIsNone(measured["memory_used_bytes"])
                self.assertIsNone(measured["memory_total_bytes"])
                self.assertEqual(measured["services_running"], 7)

    def test_a_container_without_an_id_leaves_memory_unknown(self) -> None:
        api = ListingApi([{"Names": ["/shimpz-team"]}], [], {"/info": {"MemTotal": 4 * GIB}})
        measured = self.measure(api)
        self.assertEqual(measured["services_running"], 1)
        self.assertIsNone(measured["memory_used_bytes"])
        self.assertNotIn("/info", [url for url, _params, _timeout in api.calls])

    def test_a_spent_budget_leaves_memory_unknown(self) -> None:
        clock = iter([0.0, 0.0, 0.0, 6.0])
        with mock.patch.object(lifecycle.time, "monotonic", lambda: next(clock, 6.0)):
            measured = self.measure(self.healthy())
        self.assertIsNone(measured["memory_used_bytes"])
        self.assertIsNone(measured["memory_total_bytes"])

    def test_memory_measured_past_its_budget_is_unknown(self) -> None:
        api = ListingApi([], [], {"/info": {"MemTotal": 4 * GIB}})
        clock = iter([0.0, 0.0, 0.0, 0.0, 6.0])
        with mock.patch.object(lifecycle.time, "monotonic", lambda: next(clock, 6.0)):
            measured = self.measure(api)
        self.assertIsNone(measured["memory_used_bytes"])

    def test_an_unreadable_storage_filesystem_is_null(self) -> None:
        for name, statvfs in (
            ("refused", mock.Mock(side_effect=OSError("gone"))),
            ("empty", mock.Mock(return_value=_statvfs(0, 0))),
        ):
            with self.subTest(name):
                measured = self.measure(self.healthy(), statvfs)
                self.assertIsNone(measured["storage_used_bytes"])
                self.assertIsNone(measured["storage_total_bytes"])
                self.assertEqual(measured["memory_total_bytes"], 4 * GIB)

    def test_an_unreachable_container_listing_is_docker_unavailable(self) -> None:
        for name, members, assistants in (
            ("refused", APIError("down"), []),
            ("transport", [], OSError("socket")),
            ("not a list", {"Id": "x"}, []),
            ("not objects", ["x"], []),
        ):
            with self.subTest(name), self.assertRaises(ApiProblemError) as caught:
                lifecycle.space_resources(_controller(ListingApi(members, assistants, {})))
            self.assertEqual((caught.exception.status, caught.exception.code), (503, "docker-unavailable"))

    def test_a_spent_budget_before_the_listing_is_docker_unavailable(self) -> None:
        clock = iter([0.0, 6.0])
        with (
            mock.patch.object(lifecycle.time, "monotonic", lambda: next(clock, 6.0)),
            self.assertRaises(ApiProblemError) as caught,
        ):
            lifecycle.space_resources(_controller(self.healthy()))
        self.assertEqual(caught.exception.code, "docker-unavailable")


class SpaceResourceRouteTests(unittest.TestCase):
    def handler(self, controller: object, headers: tuple[tuple[str, str], ...] = ()) -> server.Handler:
        handler = object.__new__(server.Handler)
        handler.command = "GET"
        handler.path = "/v1/space/resources"
        handler.server = SimpleNamespace(controller=controller)
        handler.headers = Message()
        for name, value in headers:
            handler.headers.add_header(name, value)
        handler.rfile = BytesIO()
        handler.wfile = BytesIO()
        return handler

    def test_a_supervisor_session_reads_the_signal(self) -> None:
        measured = {"services_running": 7}
        controller = SimpleNamespace(space_resources=mock.Mock(return_value=measured))
        evidence = authority.Evidence(
            supervisor_id="a" * 32,
            authority_kind="session",
            authority_digest="b" * 64,
            assertion_id="c" * 32,
            expires_at=2_200_000_015,
        )
        with (
            mock.patch.object(authority, "verify", return_value=evidence) as verify,
            mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32),
        ):
            result = self.handler(controller)._authorized_route(http_audit.RequestAudit())
        self.assertEqual(result, (HTTPStatus.OK, measured, "space-resources", None, None))
        binding = verify.call_args.kwargs["request"]
        self.assertEqual((binding.method, binding.path), ("GET", "/v1/space/resources"))
        self.assertEqual(binding.authority_kinds, frozenset({"session"}))

    def test_without_supervisor_authority_nothing_is_measured(self) -> None:
        controller = SimpleNamespace(space_resources=mock.Mock())
        with (
            mock.patch.object(http_audit.local_audit, "record", return_value="d" * 32),
            self.assertRaises(ApiProblemError) as caught,
        ):
            self.handler(controller)._authorized_route(http_audit.RequestAudit())
        self.assertEqual((caught.exception.status, caught.exception.code), (HTTPStatus.FORBIDDEN, "invalid-supervisor"))
        controller.space_resources.assert_not_called()


if __name__ == "__main__":
    unittest.main()
