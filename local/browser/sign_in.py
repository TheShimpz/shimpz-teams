"""A person signing in to a site in a Browser only they drive, and saving it as a Sign-in (ADR-0117, Sign-ins 2-5).

A sign-in session holds the Space's Browser in `person` mode: it starts at the URL the person chose, shows Chromium's
own address bar, runs no agent and sends nothing to Brain. The person watches and controls it through its view. It
ends by Save, Cancel, its 30-minute limit, or 10 minutes without the person in control. Save checks that one window is
open, fences the session, lets Chromium rest so its stores commit, stops it cleanly, and streams its profile through
the capsule check into a sealed draft; a profile that holds a session at a known identity provider's sign-in host is
refused, and the Sign-in is kept only if its Team is still the same incarnation and the session was not ended meanwhile.
Whatever happens, the Browser is released.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from browser import cua
from local import audit as local_audit
from local.browser import capsule as capsule_check
from local.browser import sign_ins as sign_in_store
from local.browser import view as local_view
from local.browser.runtime import PERSON, BrowserError, BrowserRuntime, Lease
from local.browser.service import BrowserService, language
from protocol.http.v1 import sign_in as sign_in_protocol

# Chromium commits its cookie store every 30 seconds; Save lets it rest that long first (owner-accepted, item 4).
REST_SECONDS = 31.0
SESSION_SECONDS = 30 * 60.0
IDLE_SECONDS = 10 * 60.0
WATCH_SECONDS = 5.0


class SignInSessionError(RuntimeError):
    """A sign-in session could not do what was asked; `code` says why, in closed words."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(slots=True)
class _Session:
    id: str
    team_id: str
    incarnation: str
    site: str
    lease: Lease
    view: local_view.View
    started: float
    ended: threading.Event = field(default_factory=threading.Event)


class SignInSessions:
    """The Space's sign-in sessions: at most one, holding the Space's one Browser."""

    def __init__(
        self,
        service: BrowserService,
        runtime: BrowserRuntime,
        store: sign_in_store.SignInStore,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._service = service
        self._runtime = runtime
        self._store = store
        self._clock = clock
        self._guard = threading.Lock()
        self._sessions: dict[str, _Session] = {}

    def start(self, team_id: str, incarnation: str, url: object, locale: str | None) -> dict[str, str]:
        """Open a sign-in session for the Team at `url`; its id and the view the person controls it through."""
        site = sign_in_protocol.start_site(url)
        if site is None:
            raise SignInSessionError(sign_in_protocol.URL_INVALID)
        try:
            lease = self._runtime.acquire(team_id, language(locale), mode=PERSON, url=str(url))
        except BrowserError as exc:
            raise SignInSessionError(exc.code) from exc
        credentials = lease.credentials
        view = local_view.View(
            lambda: self._runtime.open_relay(lease),
            lambda exec_id, deadline: self._runtime.exited(exec_id, deadline),
            lambda state: _audit(lease, f"view:{state}"),
            authorization=credentials.authorization if credentials is not None else None,
        )
        session = _Session(secrets.token_hex(16), team_id, incarnation, site, lease, view, self._clock())
        try:
            self._service.hold(team_id, f"sign-in:{session.id}", lease, view)
        except BrowserError as exc:
            self._release(lease)
            raise SignInSessionError(exc.code) from exc
        with self._guard:
            self._sessions[team_id] = session
        _audit(lease, f"started:site={site}")
        threading.Thread(target=self._watch, args=(session,), name="browser-sign-in", daemon=True).start()
        return {"sign_in_session_id": session.id, "view_id": view.id, "site": site}

    def _take(self, team_id: str, session_id: object) -> _Session:
        """End the session's hold now: no other caller and no limit can act on it afterwards."""
        with self._guard:
            session = self._sessions.get(team_id)
            if session is None or session.id != session_id:
                raise SignInSessionError(sign_in_protocol.NOT_RUNNING)
            del self._sessions[team_id]
        session.ended.set()
        self._service.drop(team_id, session.view)
        session.view.end()
        return session

    def cancel(self, team_id: str, session_id: object) -> None:
        session = self._take(team_id, session_id)
        _audit(session.lease, "cancelled")
        self._release(session.lease)

    def save(
        self, team_id: str, session_id: object, label: object, incarnation: Callable[[], str | None]
    ) -> dict[str, object]:
        """Keep the session's profile as a Sign-in; the Browser is released however it ends."""
        if sign_in_protocol.canonical_label(label) is None:
            raise SignInSessionError(sign_in_protocol.LABEL_INVALID)
        with self._guard:
            session = self._sessions.get(team_id)
            if session is None or session.id != session_id:
                raise SignInSessionError(sign_in_protocol.NOT_RUNNING)
        if self._windows(session.lease) != 1:
            raise SignInSessionError(sign_in_protocol.WINDOWS)
        session = self._take(team_id, session_id)
        draft = None
        try:
            # The rest is the session's own: nothing else acts on the Browser meanwhile.
            time.sleep(REST_SECONDS)
            self._runtime.stop(session.lease)
            draft = self._store.draft(team_id, session.incarnation)
            check = capsule_check.Check()

            def keep(chunk: bytes) -> None:
                check.feed(chunk)
                draft.add(chunk)

            self._runtime.export(session.lease, keep)
            check.finish()
            if check.identity_hosts():
                raise SignInSessionError(sign_in_protocol.IDENTITY_PROVIDER)
            saved = draft.commit(
                session.site, label, self._runtime.image or "", lambda: incarnation() == session.incarnation
            )
        except (BrowserError, capsule_check.CapsuleError, sign_in_store.SignInError) as exc:
            if draft is not None:
                draft.discard()
            _audit(session.lease, f"unsaved:{type(exc).__name__}")
            raise SignInSessionError(sign_in_protocol.UNSAVED) from exc
        except SignInSessionError as exc:
            # Only the capsule's own refusal comes here, after its draft began.
            draft.discard()
            _audit(session.lease, f"refused:{exc.code}")
            raise
        finally:
            self._release(session.lease)
        _audit(session.lease, "saved")
        return saved

    def _windows(self, lease: Lease) -> int:
        """How many Chromium windows the person's Browser shows, through a Driver session that binds nothing."""
        deadline = self._clock() + cua.CALL_SECONDS
        try:
            exec_id, stream = self._runtime.open_stream(lease, deadline, lambda: True)
        except BrowserError as exc:
            raise SignInSessionError(sign_in_protocol.UNSAVED) from exc
        try:
            session = cua.CuaSession(stream)
            session.deadline = deadline
            session.initialize()
            return session.chromium_windows()
        except cua.CuaError as exc:
            raise SignInSessionError(sign_in_protocol.UNSAVED) from exc
        finally:
            stream.close_input()
            stream.close()
            self._runtime.exited(exec_id, self._clock() + 5.0)

    def _watch(self, session: _Session) -> None:
        """End the session at its limit or once the person left it alone too long."""
        while not session.ended.wait(WATCH_SECONDS):
            if self._clock() - session.started >= SESSION_SECONDS or session.view.quiet_for() >= IDLE_SECONDS:
                try:
                    taken = self._take(session.team_id, session.id)
                except SignInSessionError:
                    return
                _audit(taken.lease, "expired", background=True)
                self._release(taken.lease, background=True)
                return

    def end_team(self, team_id: str) -> None:
        """The Team is deleted or the Space resets: its sign-in session ends at once, keeping nothing."""
        with self._guard:
            session = self._sessions.get(team_id)
        if session is not None:
            try:
                self.cancel(team_id, session.id)
            except SignInSessionError:
                return

    def end_all(self) -> None:
        """A Space reset: every sign-in session ends at once, keeping nothing."""
        with self._guard:
            teams = list(self._sessions)
        for team_id in teams:
            self.end_team(team_id)

    def _release(self, lease: Lease, *, background: bool = False) -> None:
        try:
            self._runtime.release(lease)
        except BrowserError:
            _audit(lease, "release-failed", background=background)


def _audit(lease: Lease, state: str, *, background: bool = False) -> None:
    """A sign-in session's states, never a URL path, page content, or input; Team's own when no request made them."""
    detail = f"{state}:container={lease.container_id[:12]}"
    if background:
        local_audit.record(
            "browser-sign-in", result="ok", principal=local_audit.TEAM, team_id=lease.team_id, detail=detail
        )
    else:
        local_audit.record_request("browser-sign-in", result="ok", team_id=lease.team_id, detail=detail)
