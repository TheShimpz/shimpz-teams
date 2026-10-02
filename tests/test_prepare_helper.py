"""The networkless helper envelope, its per-file process, and its removal (ADR-0093)."""

from __future__ import annotations

import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from docker.errors import DockerException, NotFound

from action import execution as action_execution
from local import prepare as local_prepare
from prepare import helper as preparation_helper
from prepare import limits

IMAGE_ID = "sha256:" + "c" * 64


class _Container:
    def __init__(self, events: list[object], name: str) -> None:
        self.id = f"helper-{name}"
        self.events = events

    def start(self) -> None:
        self.events.append(("start", self.id))

    def remove(self, *, force: bool) -> None:
        self.events.append(("remove", self.id, force))


class _Client:
    def __init__(self) -> None:
        self.events: list[object] = []
        self.created: list[dict[str, object]] = []
        self.api = SimpleNamespace()
        self.containers = SimpleNamespace(
            create=self._create,
            get=lambda _host: SimpleNamespace(image=SimpleNamespace(id=IMAGE_ID)),
            list=self._list,
        )
        self.listed: list[dict[str, object]] = []

    def _create(self, **kwargs: object) -> _Container:
        self.created.append(kwargs)
        return _Container(self.events, str(len(self.created)))

    def _list(self, **kwargs: object) -> list[_Container]:
        self.listed.append(kwargs)
        return [_Container(self.events, "stale")]


class HelperEnvelopeTests(unittest.TestCase):
    def test_the_local_helper_is_networkless_readonly_unprivileged_and_bounded(self) -> None:
        client = _Client()
        kwargs = local_prepare.helper_kwargs(client, space_id="space", team_id="team_1", cpuset_cpus="0-1")
        self.assertEqual(kwargs["image"], IMAGE_ID)
        self.assertEqual(kwargs["network_mode"], "none")
        self.assertIs(kwargs["read_only"], True)
        self.assertEqual(kwargs["cap_drop"], ["ALL"])
        self.assertEqual(kwargs["security_opt"], ["no-new-privileges:true"])
        self.assertIs(kwargs["privileged"], False)
        self.assertEqual(kwargs["user"], "65534:65534")
        self.assertEqual((kwargs["mounts"], kwargs["volumes"]), ([], {}))
        self.assertNotIn("environment", {key for key, value in kwargs.items() if "DOCKER" in str(value)})
        self.assertEqual(kwargs["mem_limit"], 256 * 1024 * 1024)
        self.assertEqual(kwargs["memswap_limit"], kwargs["mem_limit"])
        self.assertEqual(kwargs["nano_cpus"], 500_000_000)
        self.assertEqual(kwargs["pids_limit"], 128)
        self.assertEqual(kwargs["entrypoint"], preparation_helper.IDLE_ENTRYPOINT)
        self.assertEqual(kwargs["cpuset_cpus"], "0-1")
        self.assertEqual(kwargs["restart_policy"], {"Name": "no"})
        self.assertEqual(kwargs["labels"]["com.shimpz.local.kind"], "prepare")
        self.assertEqual(kwargs["labels"]["com.shimpz.local.team-id"], "team_1")

    def test_an_unknown_controller_image_refuses_preparation(self) -> None:
        for broken in (
            SimpleNamespace(containers=SimpleNamespace(get=mock.Mock(side_effect=NotFound("gone")))),
            SimpleNamespace(containers=SimpleNamespace(get=lambda _host: SimpleNamespace(image=None))),
            SimpleNamespace(containers=SimpleNamespace(get=lambda _host: SimpleNamespace(image=SimpleNamespace(id=7)))),
        ):
            with self.subTest(broken=broken), self.assertRaises(preparation_helper.HelperUnavailableError):
                preparation_helper.own_image_id(broken, (DockerException,))


class HelperSessionTests(unittest.TestCase):
    def _helper(self, client: _Client, **callbacks: object) -> preparation_helper.PreparationHelper:
        return preparation_helper.PreparationHelper(
            client, lambda: {"name": "helper"}, transport_errors=(DockerException,), **callbacks
        )

    def test_one_container_serves_each_file_with_a_fresh_bounded_process(self) -> None:
        client = _Client()
        seen: list[object] = []
        with (
            mock.patch.object(action_execution, "rpc_exchange", return_value={"type": "text", "text": "ok"}) as rpc,
            self._helper(
                client,
                started=lambda c: seen.append(("started", c.id)),
                stopped=lambda c: seen.append(("stopped", c.id)),
            ) as helper,
        ):
            self.assertEqual(helper.prepare("pdf", b"%PDF-1"), {"type": "text", "text": "ok"})
            self.assertEqual(helper.prepare("pdf", b"%PDF-2"), {"type": "text", "text": "ok"})
        self.assertEqual(len(client.created), 1)
        self.assertEqual(client.events, [("start", "helper-1"), ("remove", "helper-1", True)])
        self.assertEqual(seen, [("started", "helper-1"), ("stopped", "helper-1")])
        container_id, argv, _encoded, strategy = rpc.call_args_list[0].args
        self.assertEqual((container_id, argv), ("helper-1", preparation_helper.WORKER_ARGV))
        self.assertEqual(strategy.timeout, limits.HELPER_WALL_SECONDS)
        self.assertEqual(strategy.maximum, limits.MAX_HELPER_OUTPUT_BYTES)
        self.assertEqual((strategy.user, strategy.workdir), ("65534:65534", "/app"))

    def test_a_failed_file_is_refused_and_the_helper_is_replaced(self) -> None:
        client = _Client()

        def exchange(*_args: object) -> object:
            strategy = _args[3]
            if len(client.created) == 1:
                strategy.fail_stop()
                raise action_execution.RpcExchangeError("timeout")
            return {"type": "text", "text": "second"}

        with mock.patch.object(action_execution, "rpc_exchange", side_effect=exchange), self._helper(client) as helper:
            self.assertEqual(helper.prepare("pdf", b"x"), {"type": "opaque", "reason": "unreadable"})
            self.assertEqual(helper.prepare("pdf", b"y"), {"type": "text", "text": "second"})
        self.assertEqual(len(client.created), 2)
        self.assertIn(("remove", "helper-1", True), client.events)
        self.assertIn(("remove", "helper-2", True), client.events)

    def test_a_non_object_answer_is_unreadable(self) -> None:
        with mock.patch.object(action_execution, "rpc_exchange", return_value=[1]), self._helper(_Client()) as helper:
            self.assertEqual(helper.prepare("image", b"x"), {"type": "opaque", "reason": "unreadable"})

    def test_start_and_removal_failures_are_explicit(self) -> None:
        client = _Client()
        client.containers.create = mock.Mock(side_effect=DockerException("no capacity"))
        with self._helper(client) as helper, self.assertRaises(preparation_helper.HelperUnavailableError):
            helper.prepare("image", b"x")

        client = _Client()
        failing = _Container(client.events, "x")
        failing.remove = mock.Mock(side_effect=DockerException("busy"))
        client.containers.create = lambda **_kwargs: failing
        helper = self._helper(client)
        with mock.patch.object(action_execution, "rpc_exchange", return_value={}):
            helper.prepare("image", b"x")
        with self.assertRaises(preparation_helper.HelperUnavailableError):
            helper.close()

        gone = _Container(client.events, "y")
        gone.remove = mock.Mock(side_effect=NotFound("already removed", response=SimpleNamespace(status_code=404)))
        client.containers.create = lambda **_kwargs: gone
        helper = self._helper(client)
        with mock.patch.object(action_execution, "rpc_exchange", return_value={}):
            helper.prepare("image", b"x")
        helper.close()
        helper.close()


class LocalAdapterTests(unittest.TestCase):
    def test_local_preparation_is_serialized_across_the_controller(self) -> None:
        client = _Client()
        entered = threading.Event()
        release = threading.Event()
        order: list[str] = []

        def first() -> None:
            with local_prepare.helper(client, space_id="s", team_id="team_1", cpuset_cpus=None):
                order.append("first-in")
                entered.set()
                release.wait(5)
                order.append("first-out")

        def second() -> None:
            entered.wait(5)
            with local_prepare.helper(client, space_id="s", team_id="team_2", cpuset_cpus=None):
                order.append("second-in")

        threads = [threading.Thread(target=first), threading.Thread(target=second)]
        for thread in threads:
            thread.start()
        entered.wait(5)
        release.set()
        for thread in threads:
            thread.join(5)
        self.assertEqual(order, ["first-in", "first-out", "second-in"])

    def test_helpers_are_removed_per_team_or_for_the_whole_space(self) -> None:
        client = _Client()
        self.assertEqual(local_prepare.remove_helpers(client, "space", "team_1"), 1)
        self.assertEqual(local_prepare.remove_helpers(client, "space"), 1)
        team_filter, space_filter = (call["filters"]["label"] for call in client.listed)
        self.assertIn("com.shimpz.local.team-id=team_1", team_filter)
        self.assertIn("com.shimpz.local.kind=prepare", space_filter)
        self.assertFalse(any(label.startswith("com.shimpz.local.team-id") for label in space_filter))

        stuck = _Client()
        stuck.containers.list = lambda **_kwargs: [SimpleNamespace(remove=mock.Mock(side_effect=DockerException("x")))]
        with self.assertRaises(DockerException):
            local_prepare.remove_helpers(stuck, "space")


if __name__ == "__main__":
    unittest.main()
