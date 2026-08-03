"""Offline-safe automatic public Assistant update reconciliation for Local Spaces."""

from __future__ import annotations

import logging
import secrets
import threading
from collections.abc import Callable

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
    ) -> None:
        self._controller = controller
        self._interval_seconds = interval_seconds
        self._jitter = jitter
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
            self._thread.join(timeout=5)

    def run_once(self) -> bool:
        try:
            targets = {item.assistant_id: item for item in self._controller.developers.catalog()}
        except developers.DevelopersError:
            log.warning("Automatic Assistant update check deferred: Developers is unavailable")
            return False
        for binding in self._controller.registry.bindings():
            target = targets.get(binding.assistant_id)
            if target is None or _version(target.assistant_version) <= _binding_version(binding):
                continue
            try:
                self._controller.install_publication(
                    binding.team_id,
                    binding.assistant_id,
                    target.source_digest,
                    expected_binding_digest=binding.binding_digest,
                )
            except ApiProblem as exc:
                log.warning(
                    "Automatic Assistant update deferred for %s/%s: %s",
                    binding.team_id,
                    binding.assistant_id,
                    exc.code,
                )
        return True

    def _run(self) -> None:
        delay = 0
        while not self._stop.wait(delay):
            succeeded = self.run_once()
            if succeeded:
                delay = self._interval_seconds + self._jitter(_JITTER_SECONDS + 1)
            else:
                previous = delay if delay > 0 else self._interval_seconds
                delay = min(previous * 2, _MAX_BACKOFF_SECONDS)


def _binding_version(binding) -> tuple[int, int, int]:
    return _version(binding.resolution["assistant_version"])


def _version(value: str) -> tuple[int, int, int]:
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)
