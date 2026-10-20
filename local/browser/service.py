"""One chat turn's Browser task on the Local Team (ADR-0117 §5 to §8).

Team acquires the Space's one Browser on a fresh egress generation, opens the Cua Driver's MCP session, runs the
task loop with the turn's Admin-delivered key, ingests the downloads, and always releases the Browser, which removes
it and ends its egress generation. A Driver session that fails mid-task is replaced on the same Browser only once its
exec is proven ended, so the failed one can never act beside its replacement. A Browser that could not be proven
removed fails the task. Stop removes the running Browser at once, so none of its calls reaches the page again. Each
task is audited with its Team and incarnation, the container, the Browser address, and the egress process and
generation its audit lines carry, with its closed ending and recoveries, never a URL or page text. While a task runs,
the Supervisor may watch its screen through the task's own view, which ends with the task.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, replace

from browser import cua
from browser import ingest as browser_ingest
from browser import task as browser_task
from inference import client as brain_runtime_client
from inference.errors import BrainRuntimeError
from local import audit as local_audit
from local.browser import view as local_view
from local.browser.channel import ChannelError, ExecStream
from local.browser.runtime import BrowserError, BrowserRuntime, Lease
from protocol.http.v1 import browser_view as view_protocol
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


# Streams of watched screens the Space carries at once.
VIEWS = 2
# The deadline to prove a failed Driver session's exec ended once its channel is shut.
DRIVER_END_SECONDS = 5.0


class BrowserService:
    """Runs Browser tasks for chat turns against the Local Docker runtime and the Team's file store."""

    def __init__(self, runtime: BrowserRuntime, storage: team_storage.TeamStorage) -> None:
        self._runtime = runtime
        self._storage = storage
        self._guard = threading.Lock()
        # The lease each Team's running task holds, with its turn's token, so that turn's Stop, and only it, reaches it,
        # and the task's screen view.
        self._running: dict[str, tuple[str, Lease, local_view.View]] = {}
        # Frame requests answered at once across the Space, each held until its response is sent.
        self._views = threading.BoundedSemaphore(VIEWS)

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
            if held is not None and lease is not None:
                # From here no download of the task is stored: a store under way finished before this took the guard.
                del self._running[team_id]
                held[2].end()
        if lease is not None:
            # A failed removal leaves the release to prove it, which keeps the Space refusing tasks until it does.
            with suppress(BrowserError):
                self._runtime.kill(lease)

    @contextmanager
    def _held(self, token: str, lease: Lease) -> Iterator[None]:
        view = local_view.View(
            lambda: self._runtime.open_relay(lease),
            lambda exec_id, deadline: self._runtime.exited(exec_id, deadline),
            lambda state: _audit_view(lease, state),
        )
        with self._guard:
            self._running[lease.team_id] = (token, lease, view)
        try:
            yield
        finally:
            with self._guard:
                held = self._running.get(lease.team_id)
                if held is not None and held[2] is view:
                    del self._running[lease.team_id]
            view.end()

    def hold(self, team_id: str, token: str, lease: Lease, view: local_view.View) -> None:
        """Make `lease` the Team's running Browser, watched through `view`; refused while the Team runs another."""
        with self._guard:
            if team_id in self._running:
                raise BrowserError("browser-busy")
            self._running[team_id] = (token, lease, view)

    def drop(self, team_id: str, view: local_view.View) -> None:
        """No longer hold the Team's Browser watched through `view`; another one stays."""
        with self._guard:
            held = self._running.get(team_id)
            if held is not None and held[2] is view:
                del self._running[team_id]

    def open_view(self, team_id: str) -> str:
        """The id of the screen view of the task the Team runs now; refused when it runs none."""
        with self._guard:
            held = self._running.get(team_id)
            if held is None:
                raise BrowserError(view_protocol.NOT_RUNNING)
            return held[2].id

    @contextmanager
    def view_slot(self) -> Iterator[None]:
        """One of the Space's stream slots, held for the whole stream."""
        if not self._views.acquire(blocking=False):
            raise BrowserError(view_protocol.BUSY)
        try:
            yield
        finally:
            self._views.release()

    def carry_view(self, team_id: str, view_id: str, client, key: str, upgraded, *, control: bool = False) -> None:
        """Carry one viewer's stream of that very Browser's screen, until the stream or the Browser ends.

        `control` asks for the person's own control, which only a Browser a person may control offers.
        """
        self._view(team_id, view_id).carry(client, key, upgraded, control=control)

    def _view(self, team_id: str, view_id: str) -> local_view.View:
        with self._guard:
            held = self._running.get(team_id)
            if held is None or held[2].id != view_id:
                raise BrowserError(view_protocol.NOT_RUNNING)
            return held[2]

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
        except BrowserError as exc:
            outcome = _lost(outcome, f"runtime/{exc.code}")
        except cua.CuaError as exc:
            outcome = browser_task.Outcome("stopped") if turn.cancelled() else _lost(outcome, f"driver/{exc.code}")
        finally:
            released = self._release(lease)
            _audit(turn, lease, f"{_ending(outcome)}:released={int(released)}")
        if not released:
            outcome = _lost(outcome, "release")
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
            return _lost(outcome, "ingest"), None
        if not self._runtime.intact(lease):
            return _lost(outcome, "runtime/not-intact"), ingested
        return outcome, ingested

    def _task(
        self,
        turn: BrowserTurn,
        context: brain_runtime_client.RuntimeContext,
        request: brain_runtime_client.BrowserRequest,
        lease: Lease,
        page_language: str,
    ) -> browser_task.Outcome:
        drivers: dict[cua.CuaSession, tuple[str, ExecStream]] = {}

        def held() -> bool:
            """The turn still runs and still holds this very lease; Stop revokes it."""
            if turn.cancelled():
                return False
            with self._guard:
                running = self._running.get(lease.team_id)
            return running is not None and running[0] == turn.token and running[1] is lease

        def driver(deadline: float) -> cua.CuaSession:
            """One Driver session on the task's Browser, opened by `deadline`; none reaches a Browser Stop revoked."""
            if not held():
                raise cua.CuaError("stopped")
            exec_id, stream = self._runtime.open_stream(lease, deadline, held)
            if not held():
                stream.close()
                raise cua.CuaError("stopped")
            session = cua.CuaSession(_Held(stream, held))
            drivers[session] = (exec_id, stream)
            session.deadline = deadline
            session.open()
            return session

        def reopen(failed: cua.CuaSession, deadline: float) -> cua.CuaSession:
            """A fresh Driver session in place of `failed`, once Docker proved the failed one's exec ended."""
            exec_id, stream = drivers.pop(failed)
            stream.shutdown()
            try:
                ended = self._runtime.exited(exec_id, min(deadline, time.monotonic() + DRIVER_END_SECONDS))
            finally:
                stream.close()
            if not ended:
                raise cua.CuaError("driver-not-ended")
            try:
                return driver(deadline)
            except BrowserError as exc:
                raise cua.CuaError("driver-unavailable") from exc

        session = driver(time.monotonic() + cua.CALL_SECONDS)

        def step(payload, seconds: float):
            try:
                return turn.runtime.browser_step(context, payload, deadline=seconds)
            except BrainRuntimeError as exc:
                raise browser_task.StepFailedError(str(exc)) from exc

        return browser_task.Task(
            request.objective,
            page_language,
            session,
            step,
            turn.cancelled,
            lambda: self._runtime.intact(lease),
            reopen=reopen,
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


class _Held:
    """A Driver session's channel that sends nothing once the turn no longer holds its Browser (Stop)."""

    def __init__(self, stream: ExecStream, held: Callable[[], bool]) -> None:
        self._stream = stream
        self._held = held

    def send(self, data: bytes, deadline: float) -> None:
        if not self._held():
            raise ChannelError("the task no longer holds its Browser")
        self._stream.send(data, deadline)

    def receive_line(self, deadline: float, limit: int) -> bytes:
        return self._stream.receive_line(deadline, limit)


def _audit(turn: BrowserTurn, lease: Lease, state: str) -> None:
    egress = lease.egress
    detail = (
        f"{state}:incarnation={turn.incarnation[:12]}:container={lease.container_id[:12]}:address={lease.address}"
        f":egress={egress.container_id[:12]}@{egress.started}:generation={egress.generation}"
    )
    local_audit.record_request("browser-task", result="ok", team_id=turn.team_id, detail=detail)


def _lost(outcome: browser_task.Outcome, cause: str) -> browser_task.Outcome:
    """The task lost its Browser after `outcome`: its steps and recoveries kept, and where it was lost."""
    return replace(outcome, status="failed", summary=None, reason="browser-lost", sentence=None, cause=cause)


def _ending(outcome: browser_task.Outcome) -> str:
    """How the task ended, for its audit line: closed words and counts only, never page content."""
    ending = f"{outcome.status}:steps={outcome.steps}:recovered={outcome.recovered}"
    return ending if outcome.cause is None else f"{ending}:cause={outcome.cause}"


def _audit_view(lease: Lease, state: str) -> None:
    """A screen view's capture state under the Supervisor who asked for it, never a pixel."""
    local_audit.record_request(
        "browser-view", result="ok", team_id=lease.team_id, detail=f"{state}:container={lease.container_id[:12]}"
    )


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
