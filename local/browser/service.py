"""One chat turn's Browser task on the Local Team (ADR-0117 §5 to §8).

Team acquires the Space's one Browser on a fresh egress generation, opens the Cua Driver's MCP session, runs the
task loop with the turn's Admin-delivered key, ingests the downloads, and always releases the Browser, which removes
it and ends its egress generation. Each task is audited with its Team, container, Browser address, and egress
generation, never a URL or page text.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from browser import cua
from browser import ingest as browser_ingest
from browser import task as browser_task
from inference import client as brain_runtime_client
from inference.errors import BrainRuntimeError
from local import audit as local_audit
from local.browser.runtime import BrowserError, BrowserRuntime, Lease
from storage import files as team_storage
from storage import web as team_web


@dataclass(frozen=True, slots=True)
class BrowserTurn:
    """What one turn's task runs with: its Team, language, the Brain client, and the turn's Stop."""

    team_id: str
    locale: str | None
    runtime: brain_runtime_client.BrainRuntimeClient
    cancelled: Callable[[], bool]


def language(locale: str | None) -> str:
    """The Browser's page language for the turn's interface language."""
    return "pt-BR" if isinstance(locale, str) and locale.lower().startswith("pt") else "en"


class BrowserService:
    """Runs Browser tasks for chat turns against the Local Docker runtime and the Team's file store."""

    def __init__(self, runtime: BrowserRuntime, storage: team_storage.TeamStorage, web: team_web.WebFiles) -> None:
        self._runtime = runtime
        self._storage = storage
        self._web = web

    @property
    def available(self) -> bool:
        return self._runtime.available

    def startup(self) -> bool:
        return self._runtime.startup()

    def remove_team(self, team_id: str) -> None:
        self._runtime.remove_team(team_id)

    def clean(self) -> None:
        self._runtime.clean()

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
        _audit(turn.team_id, lease, "started")
        outcome, ingested = browser_task.Outcome("failed", reason="browser-lost"), None
        try:
            outcome = self._task(turn, context, request, lease, page_language)
            if outcome.status != "stopped":
                ingested = self._ingest(turn.team_id, lease)
        except BrowserError, cua.CuaError, browser_ingest.ListingError:
            outcome = browser_task.Outcome("failed", reason="browser-lost", steps=outcome.steps)
        finally:
            released = self._release(lease)
            _audit(turn.team_id, lease, f"{outcome.status}:steps={outcome.steps}:released={int(released)}")
        return _result(outcome, ingested), _terminal(ingested)

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

        def step(payload):
            try:
                return turn.runtime.browser_step(context, payload)
            except BrainRuntimeError as exc:
                raise browser_task.StepFailedError(str(exc)) from exc

        return browser_task.Task(request.objective, page_language, session, step, turn.cancelled).run()

    def _ingest(self, team_id: str, lease: Lease) -> browser_ingest.Ingested:
        def read(name: str, size: int) -> bytes:
            try:
                return self._runtime.read(lease, name, size)
            except BrowserError as exc:
                raise browser_ingest.ReadLostError(name) from exc

        return browser_ingest.ingest(
            self._runtime.files(lease),
            read,
            lambda digest: self._web.mark(team_id, digest),
            lambda name, data, media_type: self._storage.put(team_id, name, data, media_type),
        )

    def _release(self, lease: Lease) -> bool:
        try:
            self._runtime.release(lease)
        except BrowserError:
            # The runtime stays dirty and refuses every later task until a cleanup proves the Space clean again.
            return False
        return True


def _audit(team_id: str, lease: Lease, state: str) -> None:
    egress_id, started = lease.egress
    detail = f"{state}:container={lease.container_id[:12]}:address={lease.address}:egress={egress_id[:12]}@{started}"
    local_audit.record_request("browser-task", result="ok", team_id=team_id, detail=detail)


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
