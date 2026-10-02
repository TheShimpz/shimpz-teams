"""Opt-in proof of Action file delivery through Team's real RPC path into a real SDK Assistant (ADR-0093).

The workload runs the attachment SDK under the Local isolation envelope (128 MiB, 0.25 CPU, 64 PIDs, read-only, no
network). Run with SHIMPZ_RUN_DOCKER_TESTS=1 and SHIMPZ_SDK_WHEEL naming a built SDK wheel, for example the output of
``uv build --wheel`` in the SDK's ``python`` directory. Each case prints its measurements: wall time, the longest
silent interval on the exec socket, the controller's traced peak, and the workload cgroup's ``memory.peak``.
"""

from __future__ import annotations

import base64
import contextlib
import itertools
import json
import os
import shutil
import tempfile
import time
import tracemalloc
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from action import execution as action_execution
from action import files as action_files
from action import human as action_human
from assistant import spec as assistant_spec
from local.assistant import isolation
from local.assistant import rpc as local_assistant_rpc
from storage import files as team_storage

ENABLED = os.environ.get("SHIMPZ_RUN_DOCKER_TESTS") == "1" and bool(os.environ.get("SHIMPZ_SDK_WHEEL"))
FIXTURE = Path(__file__).resolve().parent / "fixtures" / "file-assistant"
IMAGE = "shimpz-test-file-assistant:local"
MIB = 1024 * 1024
# The SDK's canonical publishable icon (a blank 1024x1024 PNG).
ICON = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAABAAAAAQAAQAAAABXZhYuAAAAlklEQVR42u3BAQEAAACCIP+vbkhAAQAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    "AAAAAAAAAAAAAAAAAAAAAADvBgQeAAEN3jhkAAAAAElFTkSuQmCC"
)


class _RecordingSocket:
    """The exec socket, recording when each send or receive moved bytes."""

    def __init__(self, raw: object, events: list[float]) -> None:
        self._raw = raw
        self._events = events

    def __getattr__(self, name: str) -> object:
        return getattr(self._raw, name)

    def send(self, data: bytes) -> int:
        sent = self._raw.send(data)
        self._events.append(time.monotonic())
        return sent

    def recv(self, size: int) -> bytes:
        chunk = self._raw.recv(size)
        self._events.append(time.monotonic())
        return chunk


@unittest.skipUnless(ENABLED, "real Docker test is opt-in")
class RealFileDeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        import docker

        cls.client = docker.from_env()
        with tempfile.TemporaryDirectory() as context:
            root = Path(context)
            shutil.copytree(FIXTURE, root, dirs_exist_ok=True)
            (root / "project" / "icon.png").write_bytes(ICON)
            (root / "wheel").mkdir()
            shutil.copy(os.environ["SHIMPZ_SDK_WHEEL"], root / "wheel")
            cls.client.images.build(path=str(root), tag=IMAGE, rm=True)
        contract = cls.client.containers.run(
            IMAGE,
            ["-m", "shimpz._bridge", "contract", "/opt/shimpz"],
            entrypoint="/opt/shimpz/runtime/bin/python3",
            network_mode="none",
            remove=True,
        )
        cls.contract = json.loads(contract)
        cls.action = assistant_spec.action_spec(cls.contract["actions"][0])
        cls.catalog = action_human.catalog_by_id(cls.contract)

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.storage = team_storage.TeamStorage(Path(temporary.name) / "teams", limit_bytes=64 * MIB)
        self.container = self.client.containers.run(
            IMAGE,
            detach=True,
            user=isolation.ASSISTANT_UID,
            network_mode="none",
            read_only=True,
            tmpfs=dict(isolation.ASSISTANT_TMPFS),
            mem_limit=isolation.ASSISTANT_MEMORY,
            memswap_limit=isolation.ASSISTANT_MEMORY,
            nano_cpus=isolation.ASSISTANT_NANO_CPUS,
            pids_limit=isolation.ASSISTANT_PIDS,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges"],
            ulimits=None,
        )
        self.addCleanup(self.container.remove, force=True)
        self.controller = SimpleNamespace(
            client=self.client,
            _fail_stop_action=lambda container: container.kill(),
            _close_exec_stream=action_execution.close_exec_stream,
        )

    def _rpc(self, file: action_files.ActionFile, transcript: action_human.ActionTranscript):
        """One invocation through Team's real Local RPC path, returning its measurements."""
        events: list[float] = []
        original = action_execution.exchange_rpc_frames

        def recorded(raw_socket, data, deadline, maximum):
            return original(_RecordingSocket(raw_socket, events), data, deadline, maximum)

        action_input = {"document": file.id}
        tracemalloc.start()
        started = time.monotonic()
        try:
            # Exactly as a chat batch does: the file-RPC slot is admitted before any byte is read.
            admission = action_files.admitted(file, self.action.human_requests, transcript, lambda: False)
            admission.__enter__()
            files = action_files.deliver(
                self.action, file, transcript, action_input, lambda file_id: self.storage.get("team_1", file_id)
            )
            payload = {
                "input": action_input,
                "integrations": {},
                "stored_inputs": {},
                "files": files,
                "operation_id": action_execution.action_journal.new_operation_id(),
            }
            if transcript.responses:
                payload["responses"] = transcript.payloads()
            with mock.patch.object(action_execution, "exchange_rpc_frames", recorded):
                raw = local_assistant_rpc._rpc(self.controller, self.container, "store", payload)
            finished = time.monotonic()
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            admission.__exit__(None, None, None)
            tracemalloc.stop()
        moments = [started, *events, finished]
        measured = {
            "wall_seconds": round(finished - started, 3),
            "longest_silence_seconds": round(max(b - a for a, b in itertools.pairwise(moments)), 3),
            "controller_traced_peak_mib": round(peak / MIB, 1),
            "workload_memory_peak_mib": round(self._workload_peak() / MIB, 1),
        }
        return raw, files, measured

    def _workload_peak(self) -> int:
        result = self.container.exec_run(
            ["/usr/local/bin/python3", "-c", "print(open('/sys/fs/cgroup/memory.peak').read().strip())"],
            user=isolation.ASSISTANT_UID,
        )
        return int(result.output)

    def _suspension(self, raw: object) -> action_human.HumanRequest:
        policy = action_execution.RpcResultPolicy(
            human_requests=self.action.human_requests, catalog=self.catalog, file_withheld=True
        )
        with self.assertRaises(action_human.HumanRequestSuspensionError) as suspended:
            action_execution.project_rpc_result(raw, {}, lambda value: value, policy)
        return suspended.exception.request

    def _file(self, size: int) -> action_files.ActionFile:
        stored = self.storage.put("team_1", "report.pdf", os.urandom(size), "application/pdf")
        return action_files.selected([stored])[stored["id"]]

    def _deliver(self, size: int) -> dict[str, object]:
        file = self._file(size)
        raw, files, withheld = self._rpc(file, action_human.ActionTranscript("interrupt-1"))
        self.assertEqual(files[file.id]["content"], {"type": "withheld"})
        approved = action_human.ActionTranscript("interrupt-1").append(self._suspension(raw), True)
        raw, files, delivered = self._rpc(file, approved)
        self.assertEqual(files[file.id]["content"]["type"], "delivered")
        self.assertEqual(raw, {"type": "result", "result": {"bytes": size, "sha256": file.sha256}})
        self.assertLess(delivered["wall_seconds"], action_files.FILE_RPC_TIMEOUT_SECONDS)
        self.assertLess(delivered["workload_memory_peak_mib"], isolation.ASSISTANT_MEMORY / MIB)
        return {"size_mib": round(size / MIB, 2), "withheld": withheld, "delivered": delivered}

    def test_an_eight_mib_file_is_delivered_after_approval_within_the_deadline(self) -> None:
        print(json.dumps({"case": "8 MiB", **self._deliver(8 * MIB)}))

    def test_a_one_mib_file_is_delivered_after_approval(self) -> None:
        print(json.dumps({"case": "1 MiB", **self._deliver(MIB)}))

    def test_a_denied_file_never_leaves_the_controller(self) -> None:
        file = self._file(8 * MIB)
        raw, files, withheld = self._rpc(file, action_human.ActionTranscript("interrupt-1"))
        self.assertEqual(files[file.id]["content"], {"type": "withheld"})
        self._suspension(raw)
        # A denial ends the turn: Team never replays, so the only invocation the workload saw carried no bytes.
        self.assertLess(withheld["workload_memory_peak_mib"], 64)
        with contextlib.suppress(Exception):
            self.container.reload()
        self.assertEqual(self.container.status, "running")
        print(json.dumps({"case": "8 MiB withheld then denied", "withheld": withheld}))


if __name__ == "__main__":
    unittest.main()
