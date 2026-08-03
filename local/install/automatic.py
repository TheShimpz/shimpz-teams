"""Offline-safe automatic public Assistant update reconciliation for Local Spaces."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable

from docker.errors import DockerException

from install import bindings
from local.errors import ApiProblemError as ApiProblem
from local.install import developers

_INTERVAL_SECONDS = 300
_MAX_BACKOFF_SECONDS = 3600
_JITTER_SECONDS = 60
log = logging.getLogger("shimpz.team.local.install.automatic")


class AutomaticAssistantUpdater:
    def __init__(
        self,
        controller,
        *,
        interval_seconds: int = _INTERVAL_SECONDS,
        jitter: Callable[[int], int] = secrets.randbelow,
        record: Callable[[str | None, str | None, str, str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._controller = controller
        self._interval_seconds = interval_seconds
        self._jitter = jitter
        self._record = record
        self._clock = clock
        self._failures: dict[tuple[str, str, str], int] = {}
        self._retry_after: dict[tuple[str, str, str], float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("automatic Assistant updater is already running")
        self._thread = threading.Thread(target=self._run, name="assistant-updates", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                log.error("Automatic Assistant updater did not stop before shutdown")

    def run_once(self) -> bool:
        self._controller.assistant_lifecycle.sweep_residues()
        try:
            targets = {item.assistant_id: item for item in self._controller.developers.catalog()}
            installed = self._controller.registry.bindings()
        except (developers.DevelopersError, bindings.DynamicAssistantError):
            log.warning("Automatic Assistant update check deferred: Developers is unavailable")
            self._record_result(None, None, "error", "cycle:unavailable")
            return False
        active_keys = {
            (binding.team_id, binding.assistant_id, binding.binding_digest)
            for binding in installed
        }
        self._failures = {key: value for key, value in self._failures.items() if key in active_keys}
        self._retry_after = {key: value for key, value in self._retry_after.items() if key in active_keys}
        now = self._clock()
        for binding in installed:
            key = binding.team_id, binding.assistant_id, binding.binding_digest
            if now < self._retry_after.get(key, 0):
                continue
            target = targets.get(binding.assistant_id)
            try:
                current_version = _binding_version(binding)
            except (KeyError, TypeError, ValueError):
                log.exception("Automatic Assistant update check found an invalid installed binding")
                self._record_result(binding.team_id, binding.assistant_id, "error", "binding:invalid")
                continue
            if target is None or _version(target.assistant_version) <= current_version:
                self._clear_failure(key)
                continue
            try:
                self._controller.install_publication(
                    binding.team_id,
                    binding.assistant_id,
                    target.source_digest,
                    expected_binding_digest=binding.binding_digest,
                )
            except (ApiProblem, DockerException, bindings.DynamicAssistantError) as exc:
                code = exc.code if isinstance(exc, ApiProblem) else "runtime-unavailable"
                log.warning(
                    "Automatic Assistant update deferred for %s/%s: %s",
                    binding.team_id,
                    binding.assistant_id,
                    code,
                )
                self._record_result(binding.team_id, binding.assistant_id, "error", f"deferred:{code}")
                self._defer(key, now)
            else:
                self._clear_failure(key)
                self._record_result(
                    binding.team_id,
                    binding.assistant_id,
                    "ok",
                    f"updated:{binding.resolution['assistant_version']}:{target.assistant_version}",
                )
        return True

    def _defer(self, key: tuple[str, str, str], now: float) -> None:
        failures = self._failures.get(key, 0) + 1
        self._failures[key] = failures
        delay = min(self._interval_seconds * (2 ** min(failures - 1, 4)), _MAX_BACKOFF_SECONDS)
        self._retry_after[key] = now + delay

    def _clear_failure(self, key: tuple[str, str, str]) -> None:
        self._failures.pop(key, None)
        self._retry_after.pop(key, None)

    def _run(self) -> None:
        delay = 0
        while not self._stop.wait(delay):
            try:
                succeeded = self.run_once()
            except (RuntimeError, DockerException, KeyError, TypeError, ValueError):
                log.exception("Automatic Assistant updater recovered from an unexpected failure")
                self._record_result(None, None, "error", "thread:runtime-unavailable")
                succeeded = False
            if succeeded:
                delay = self._interval_seconds + self._jitter(_JITTER_SECONDS + 1)
            else:
                previous = delay if delay > 0 else self._interval_seconds
                delay = min(previous * 2, _MAX_BACKOFF_SECONDS)

    def _record_result(
        self,
        team_id: str | None,
        assistant_id: str | None,
        result: str,
        detail: str,
    ) -> None:
        if self._record is None:
            return
        try:
            self._record(team_id, assistant_id, result, detail)
        except RuntimeError:
            log.exception("Automatic Assistant update audit could not be recorded")


def _binding_version(binding) -> tuple[int, int, int]:
    return _version(binding.resolution["assistant_version"])


def _version(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)
