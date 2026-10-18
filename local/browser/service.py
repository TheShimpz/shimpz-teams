"""One chat turn's Browser task on the Local Team (ADR-0117 §5 to §8).

Team acquires the Space's one Browser on a fresh egress generation, opens the Cua Driver's MCP session, runs the
task loop with the turn's Admin-delivered key, ingests the downloads, and always releases the Browser, which removes
it and ends its egress generation. A Browser that could not be proven removed fails the task. Stop removes the running
Browser at once, so none of its calls reaches the page again. Each task is audited with its Team and incarnation, the
container, the Browser address, and the egress process and generation its audit lines carry, never a URL or page text.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass

from browser import cua
from browser import ingest as browser_ingest
from browser import task as browser_task
from inference import client as brain_runtime_client
from inference.errors import BrainRuntimeError
from local import audit as local_audit
from local.browser.runtime import BrowserError, BrowserRuntime, Lease
from storage import files as team_storage


@dataclass(frozen=True, slots=True)
class BrowserTurn:
    """What one turn's task runs with: its Team and incarnation, the turn's token, language, Brain client, and Stop."""

    team_id: str
    incarnation: str
    token: str
    locale: str | None
    runtime: brain_runtime_client.BrainRuntimeClient
    cancelled: Callable[[], bool]


def language(locale: str | None) -> str:
    """The Browser's page language for the turn's interface language."""
    return "pt-BR" if isinstance(locale, str) and locale.lower().startswith("pt") else "en"


class BrowserService:
    """Runs Browser tasks for chat turns against the Local Docker runtime and the Team's file store."""

    def __init__(self, runtime: BrowserRuntime, storage: team_storage.TeamStorage) -> None:
        self._runtime = runtime
        self._storage = storage
        self._guard = threading.Lock()
        # The lease each Team's running task holds, with its turn's token, so that turn's Stop, and only it, reaches it.
        self._running: dict[str, tuple[str, Lease]] = {}

    @property
    def available(self) -> bool:
        return self._runtime.available

    def startup(self) -> bool:
        return self._runtime.startup()

    def remove_team(self, team_id: str) -> None:
        self._runtime.remove_team(team_id)

    def clean(self) -> None:
        self._runtime.clean()

    def interrupt(self, team_id: str, token: str) -> None:
        """Stop of the turn `token`: remove its running Browser at once; the task ends stopped, its release proves it.

        A turn that started since keeps its own Browser.
        """
        with self._guard:
            held = self._running.get(team_id)
            lease = held[1] if held is not None and held[0] == token else None
            if lease is not None:
                # From here no download of the task is stored: a store under way finished before this took the guard.
                del self._running[team_id]
        if lease is not None:
            # A failed removal leaves the release to prove it, which keeps the Space refusing tasks until it does.
            with suppress(BrowserError):
                self._runtime.kill(lease)

    @contextmanager
    def _held(self, token: str, lease: Lease) -> Iterator[None]:
        with self._guard:
            self._running[lease.team_id] = (token, lease)
        try:
            yield
        finally:
            with self._guard:
                self._running.pop(lease.team_id, None)

    def run(
        self,
        turn: BrowserTurn,
        context: brain_runtime_client.RuntimeContext,
        request: brain_runtime_client.BrowserRequest,
    ) -> tuple[dict[str, object], dict[str, object]]:
        """The result the Brain resumes with, and the terminal's `browser` field."""
        page_language = language(turn.locale)
        try:
            lease = self._runtime.acquire(turn.team_id, page_language)
        except BrowserError as exc:
            local_audit.record_request("browser-task", result="denied", team_id=turn.team_id, detail=exc.code)
            return _result(browser_task.Outcome("failed", reason=exc.code), None), _terminal(None)
        outcome, ingested = browser_task.Outcome("failed", reason="browser-lost"), None
        # From the moment it is held, the lease is released however the task ends, its audit included.
        try:
            with self._held(turn.token, lease):
                outcome, ingested = self._work(turn, context, request, lease, page_language)
        except BrowserError, cua.CuaError:
            outcome = browser_task.Outcome("failed", reason="browser-lost", steps=outcome.steps)
        finally:
            released = self._release(lease)
            _audit(turn, lease, f"{outcome.status}:steps={outcome.steps}:released={int(released)}")
        if not released:
            outcome = browser_task.Outcome("failed", reason="browser-lost", steps=outcome.steps)
        return _result(outcome, ingested), _terminal(ingested)

    def _work(
        self,
        turn: BrowserTurn,
        context: brain_runtime_client.RuntimeContext,
        request: brain_runtime_client.BrowserRequest,
        lease: Lease,
        page_language: str,
    ) -> tuple[browser_task.Outcome, browser_ingest.Ingested | None]:
        """The task and then its downloads, each only while the turn runs; an egress replaced meanwhile loses it."""
        _audit(turn, lease, "started")
        # A Stop during creation found no lease to remove: no channel opens once the turn is stopped.
        if turn.cancelled():
            return browser_task.Outcome("stopped"), None
        outcome = self._task(turn, context, request, lease, page_language)
        if outcome.status == "stopped" or turn.cancelled():
            return outcome, None
        try:
            ingested = self._ingest(turn, lease)
        except BrowserError, browser_ingest.ListingError:
            return browser_task.Outcome("failed", reason="browser-lost", steps=outcome.steps), None
        if not self._runtime.intact(lease):
            return browser_task.Outcome("failed", reason="browser-lost", steps=outcome.steps), ingested
        return outcome, ingested

    def _task(
        self,
        turn: BrowserTurn,
        context: brain_runtime_client.RuntimeContext,
        request: brain_runtime_client.BrowserRequest,
        lease: Lease,
        page_language: str,
    ) -> browser_task.Outcome:
        session = cua.CuaSession(self._runtime.open_stream(lease))
        session.open()

        def step(payload, seconds: float):
            try:
                return turn.runtime.browser_step(context, payload, deadline=seconds)
            except BrainRuntimeError as exc:
                raise browser_task.StepFailedError(str(exc)) from exc

        return browser_task.Task(
            request.objective, page_language, session, step, turn.cancelled, lambda: self._runtime.intact(lease)
        ).run()

    def _ingest(self, turn: BrowserTurn, lease: Lease) -> browser_ingest.Ingested:
        def read(name: str, size: int) -> bytes:
            try:
                return self._runtime.read(lease, name, size)
            except BrowserError as exc:
                raise browser_ingest.ReadLostError(name) from exc

        def put(name: str, data: bytes, media_type: str) -> dict[str, object]:
            # Under the guard Stop takes to remove the Browser, so no download is stored once Stop has done so.
            with self._guard:
                held = self._running.get(turn.team_id)
                if turn.cancelled() or held is None or held[0] != turn.token:
                    raise browser_ingest.StoppedError(name)
                return self._storage.put(turn.team_id, name, data, media_type, web=True)

        return browser_ingest.ingest(self._runtime.files(lease), read, put, turn.cancelled)

    def _release(self, lease: Lease) -> bool:
        try:
            self._runtime.release(lease)
        except BrowserError:
            # The runtime stays dirty and refuses every later task until a cleanup proves the Space clean again.
            return False
        return True


def _audit(turn: BrowserTurn, lease: Lease, state: str) -> None:
    egress = lease.egress
    detail = (
        f"{state}:incarnation={turn.incarnation[:12]}:container={lease.container_id[:12]}:address={lease.address}"
        f":egress={egress.container_id[:12]}@{egress.started}:generation={egress.generation}"
    )
    local_audit.record_request("browser-task", result="ok", team_id=turn.team_id, detail=detail)


def _result(outcome: browser_task.Outcome, ingested: browser_ingest.Ingested | None) -> dict[str, object]:
    """What the chat agent reads of the task: its ending, the agent's summary, and each download's outcome."""
    status = {"done": "done", "needs-person": "needs-person", "stopped": "stopped"}.get(outcome.status, "failed")
    result: dict[str, object] = {"status": status}
    if outcome.summary is not None:
        result["summary"] = outcome.summary
    if outcome.reason is not None:
        result["reason"] = outcome.reason
    if outcome.sentence is not None:
        result["sentence"] = outcome.sentence
    kept = [] if ingested is None else [{**item, "outcome": "kept"} for item in ingested.kept]
    lost = [] if ingested is None else list(ingested.lost)
    result["files"] = [
        *({key: item[key] for key in ("name", "media_type", "size", "outcome")} for item in kept),
        *({"name": item["name"], "outcome": item["reason"]} for item in lost),
    ]
    return result


def _terminal(ingested: browser_ingest.Ingested | None) -> dict[str, object]:
    if ingested is None:
        return {"files": [], "lost": [], "skipped": 0}
    return {"files": list(ingested.kept), "lost": list(ingested.lost), "skipped": ingested.skipped}
