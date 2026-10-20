"""A person signing in to a site in a Browser only they drive, and saving it as a Sign-in (ADR-0117, Sign-ins 2-5).

A sign-in session holds the Space's Browser in `person` mode: it starts at the URL the person chose, shows Chromium's
own address bar, runs no agent and sends nothing to Brain. The person controls it through its private view, which no
one can watch. It ends by Save, Cancel, its 30-minute limit, or 10 minutes without the person connected to it.

Save checks that one window is open, then fences the session: the person's control ends and its release must be
proven before anything else acts, and the window count must still be one. Chromium rests so its stores commit, stops
cleanly, and its profile streams through the capsule check into a sealed draft; a profile with a session at a known
identity provider's sign-in host is refused. While it saves, the session stays registered: Cancel, the limit, the
Team's deletion, or a reset still end it, and then nothing is kept. The Sign-in is committed only if the session was
not ended meanwhile and its Team is still the same incarnation. Whatever happens, the Browser is released.

A session that began starting before its Team was deleted or the Space reset is never registered: each end bumps an
epoch that a starting session must still see unchanged once its Browser exists.
"""

from __future__ import annotations

import contextlib
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
# How long Save waits for the person's control to end and its release to be proven.
QUIESCE_SECONDS = 15.0
# The longest a Cancel waits for a Save under way to release the Browser: its quiesce, stop, export, and removal.
SAVE_SECONDS = 150.0
UNAVAILABLE = "browser-unavailable"
# A Browser whose removal failed is cleaned again in the background this often, until its absence is proven.
RECOVER_SECONDS = 30.0
TEAM_NOT_FOUND = "team-not-found"

# The Team's current incarnation (its network id), or None once it is gone.
Incarnation = Callable[[], str | None]


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
    # The Team's Sign-in, checked out to this session before its Browser started.
    checkout: sign_in_store.Checkout
    # Set once the session ends, however it ends; a Save under way sees it and keeps nothing.
    ended: threading.Event = field(default_factory=threading.Event)
    # Under the sessions' guard: a Save holds the session, then commits it unless it was cancelled first.
    saving: bool = False
    cancelled: bool = False
    committed: bool = False
    # Set once a Save released the Browser, `removed` saying whether its absence was proven.
    released: threading.Event = field(default_factory=threading.Event)
    removed: bool = False


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
        # Bumped by every end of a Team's sessions (its deletion) and of all of them (a reset).
        self._epochs: dict[str, int] = {}
        self._epoch = 0

    def _admission(self, team_id: str) -> tuple[int, int]:
        with self._guard:
            return self._epoch, self._epochs.get(team_id, 0)

    def start(self, team_id: str, incarnation: Incarnation, url: object, locale: str | None) -> dict[str, str]:
        """Open a sign-in session for the Team at `url`; its id and the view the person controls it through."""
        site = sign_in_protocol.start_site(url)
        if site is None:
            raise SignInSessionError(sign_in_protocol.URL_INVALID)
        admission = self._admission(team_id)
        current = incarnation()
        if current is None:
            raise SignInSessionError(TEAM_NOT_FOUND)
        event = secrets.token_hex(16)
        # Starting delegates the Team's Sign-in to a person: nothing starts unless that is durably recorded first.
        if not _activity(team_id, "browser-sign-in-start", "requested", event, flush=True):
            raise SignInSessionError(UNAVAILABLE)
        checkout = self._checkout(team_id, current)
        try:
            capsule = self._store.capsule(checkout)
            lease = self._runtime.acquire(team_id, language(locale), mode=PERSON, url=str(url), capsule=capsule)
        except (BrowserError, sign_in_store.SignInError) as exc:
            self._store.finish(checkout)
            _activity(team_id, "browser-sign-in-start", "failed", event, generation=checkout.generation)
            raise SignInSessionError(exc.code or UNAVAILABLE) from exc
        credentials = lease.credentials
        view = local_view.View(
            lambda: self._runtime.open_relay(lease),
            lambda exec_id, deadline: self._runtime.exited(exec_id, deadline),
            lambda state: _audit(lease, f"view:{state}"),
            authorization=credentials.authorization if credentials is not None else None,
            private=True,
        )
        session = _Session(secrets.token_hex(16), team_id, current, site, lease, view, self._clock(), checkout)
        try:
            self._service.hold(team_id, f"sign-in:{session.id}", lease, view)
        except BrowserError as exc:
            self._release(lease)
            self._store.finish(checkout)
            raise SignInSessionError(exc.code) from exc
        with self._guard:
            admitted = (self._epoch, self._epochs.get(team_id, 0)) == admission
            if admitted:
                self._sessions[team_id] = session
        if not admitted or incarnation() != current:
            # The Team was deleted or the Space reset while this Browser started: it never becomes a session. Its
            # removal has one owner: this start, unless a Cancel or the Team's end already took it.
            with self._guard:
                owned = not admitted or self._sessions.get(team_id) is session
                if admitted and owned:
                    del self._sessions[team_id]
            if owned:
                session.ended.set()
                self._fence(session)
                self._release(session.lease)
                self._store.finish(checkout)
            raise SignInSessionError(sign_in_protocol.NOT_RUNNING)
        _activity(team_id, "browser-sign-in-start", "completed", event, generation=checkout.generation)
        _audit(lease, f"started:site={site}")
        threading.Thread(target=self._watch, args=(session,), name="browser-sign-in", daemon=True).start()
        return {"sign_in_session_id": session.id, "view_id": view.id, "site": site}

    def _checkout(self, team_id: str, incarnation: str) -> sign_in_store.Checkout:
        try:
            return self._store.checkout(team_id, incarnation, sign_in_store.PERSON)
        except sign_in_store.SignInError as exc:
            raise SignInSessionError(exc.code or UNAVAILABLE) from exc

    def _fence(self, session: _Session) -> None:
        """No one acts on the session's Browser any more: its hold and its view end."""
        self._service.drop(session.team_id, session.view)
        session.view.end()

    def _end(self, team_id: str, session_id: object) -> tuple[_Session, bool]:
        """End the session: it, and whether a Save holds it.

        A session not saving is taken at once, for its caller to release. One saving is marked cancelled instead, so its
        Save keeps nothing and releases the Browser itself; a Save that already committed is no longer ended.
        """
        with self._guard:
            session = self._sessions.get(team_id)
            if session is None or session.id != session_id or session.committed:
                raise SignInSessionError(sign_in_protocol.NOT_RUNNING)
            if session.saving:
                session.cancelled = True
                session.ended.set()
                return session, True
            del self._sessions[team_id]
        session.ended.set()
        self._fence(session)
        return session, False

    def cancel(self, team_id: str, session_id: object, *, background: bool = False) -> None:
        """End the session, keeping nothing, once its Browser was proven removed; else it is refused as unavailable.

        A Save under way removes the Browser itself: Cancel answers once it did. A removal that failed leaves the
        Space's Browser to be cleaned and proven absent before the next one starts.
        """
        session, saving = self._end(team_id, session_id)
        _audit(session.lease, "cancelled", background=background)
        if saving:
            removed = session.released.wait(SAVE_SECONDS) and session.removed
        else:
            removed = self._release(session.lease, background=background)
            self._store.finish(session.checkout)
        _activity(
            team_id,
            "browser-sign-in-cancel",
            "completed",
            session.id,
            generation=session.checkout.generation,
            background=background,
        )
        if not removed:
            raise SignInSessionError(UNAVAILABLE)

    def save(self, team_id: str, session_id: object, incarnation: Incarnation) -> dict[str, object]:
        """Keep the session's profile as the Team's Sign-in's next generation; the Browser is released however it ends.

        Saving delegates the logins it holds, so nothing is saved unless that is durably recorded first. A completion
        that could not be recorded after the commit is answered as committed, with `recorded` false (ADR-0120).
        """
        session = self._running(team_id, session_id)
        if self._windows(session.lease) != 1:
            raise SignInSessionError(sign_in_protocol.WINDOWS)
        if not _activity(team_id, "browser-sign-in-save", "requested", session.id, flush=True):
            raise SignInSessionError(UNAVAILABLE)
        with self._guard:
            if self._sessions.get(team_id) is not session or session.saving or session.ended.is_set():
                raise SignInSessionError(sign_in_protocol.NOT_RUNNING)
            session.saving = True
        saved = None
        try:
            saved = self._persist(session, incarnation)
        finally:
            # Nothing else takes a saving session away: it leaves here, ended.
            with self._guard:
                del self._sessions[team_id]
            session.ended.set()
            self._fence(session)
            session.removed = self._release(session.lease)
            if saved is None:
                self._store.finish(session.checkout)
                _activity(team_id, "browser-sign-in-save", "failed", session.id)
            session.released.set()
        recorded = _activity(
            team_id, "browser-sign-in-save", "completed", session.id, generation=int(saved["generation"]), flush=True
        )
        _audit(session.lease, f"saved:released={int(session.removed)}")
        return {"sign_in": saved, "recorded": recorded}

    def _running(self, team_id: str, session_id: object) -> _Session:
        with self._guard:
            session = self._sessions.get(team_id)
            if session is None or session.id != session_id or session.saving:
                raise SignInSessionError(sign_in_protocol.NOT_RUNNING)
            return session

    def _persist(self, session: _Session, incarnation: Incarnation) -> dict[str, object]:
        """Fence, rest, stop, export, check, and commit; anything short of a commit keeps nothing."""
        self._fence(session)
        draft = None
        try:
            # Nothing acts on the Browser until the person's control ended and what they held was proven released.
            if not session.view.quiesce(self._clock() + QUIESCE_SECONDS):
                raise BrowserError("browser-lost")
            if self._windows(session.lease) != 1:
                raise BrowserError("browser-lost")
            # The rest is the session's own; an end meanwhile (Cancel, the limit, the Team's deletion) cuts it short.
            if session.ended.wait(REST_SECONDS):
                raise SignInSessionError(sign_in_protocol.NOT_RUNNING)
            self._runtime.stop(session.lease)
            draft = self._store.draft(session.checkout)
            check = capsule_check.Check()

            def keep(chunk: bytes) -> None:
                check.feed(chunk)
                draft.add(chunk)

            self._runtime.export(session.lease, keep)
            check.finish()
            if check.identity_hosts():
                raise SignInSessionError(sign_in_protocol.IDENTITY_PROVIDER)
            sites, truncated = check.sites()
            return draft.commit(
                sites,
                truncated,
                self._runtime.image or "",
                lambda: incarnation() == session.incarnation and self._commits(session),
            )
        except SignInSessionError as exc:
            if draft is not None:
                draft.discard()
            _audit(session.lease, f"refused:{exc.code}")
            raise
        except Exception as exc:
            if draft is not None:
                draft.discard()
            _audit(session.lease, f"unsaved:{type(exc).__name__}")
            raise SignInSessionError(sign_in_protocol.UNSAVED) from exc

    def _commits(self, session: _Session) -> bool:
        """Under the store's commit: whether the session may still be kept, after which it can no longer be ended."""
        with self._guard:
            if session.cancelled:
                return False
            session.committed = True
            return True

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

    def delete(self, team_id: str, confirmed: dict[str, object]) -> str:
        """Delete the Team's Sign-in exactly as the Supervisor confirmed it with a second factor (ADR-0117 item 6).

        Team re-checks the confirmed Sign-in, generation, and incarnation, fences the Sign-in durably, ends a session
        using it, and removes everything: `deleted` once proven, else `pending` while it retries in the background.
        """
        event, generation = secrets.token_hex(16), int(confirmed["generation"])
        details = {"generation": generation, "factor": str(confirmed["factor"])}
        # Deleting is protective: an activity journal that cannot record it never stops it (ADR-0120).
        _activity(team_id, "browser-sign-in-delete", "requested", event, **details)
        expected = (str(confirmed["sign_in_id"]), generation, str(confirmed["team_incarnation"]))
        try:
            self._store.begin_delete(team_id, expected)
        except sign_in_store.SignInError as exc:
            _activity(team_id, "browser-sign-in-delete", "failed", event, **details)
            raise SignInSessionError(exc.code or UNAVAILABLE) from exc
        with self._guard:
            session = self._sessions.get(team_id)
        if session is not None:
            # The Browser of a session using the Sign-in ends now; the fence already keeps its Save from committing.
            with contextlib.suppress(SignInSessionError):
                self.cancel(team_id, session.id)
        if self._deleted(team_id):
            _activity(team_id, "browser-sign-in-delete", "completed", event, **details)
            return "deleted"
        threading.Thread(
            target=self._delete_later, args=(team_id, event, details), name="browser-sign-in-delete", daemon=True
        ).start()
        return "pending"

    def _deleted(self, team_id: str) -> bool:
        try:
            return self._store.complete_delete(team_id)
        except sign_in_store.SignInError:
            return False

    def _delete_later(self, team_id: str, event: str, details: dict[str, object]) -> None:
        """Retry a fenced deletion until its absence is proven; a restarted Team process completes it at its start."""
        while True:
            time.sleep(RECOVER_SECONDS)
            if self._deleted(team_id):
                _activity(team_id, "browser-sign-in-delete", "completed", event, background=True, **details)
                return

    def _watch(self, session: _Session) -> None:
        """End the session at its limit, once the person left it alone too long, or once its screen is lost.

        A screen is lost when its Browser failed: a session no one can drive any more is never left running.
        """
        while not session.ended.wait(WATCH_SECONDS):
            lost = session.view.unavailable
            if lost or self._clock() - session.started >= SESSION_SECONDS or session.view.quiet_for() >= IDLE_SECONDS:
                _audit(session.lease, "lost" if lost else "expired", background=True)
                try:
                    self.cancel(session.team_id, session.id, background=True)
                except SignInSessionError:
                    return
                return

    def end_team(self, team_id: str) -> None:
        """The Team is deleted: its sign-in session ends at once, keeping nothing, and one still starting never runs."""
        with self._guard:
            self._epochs[team_id] = self._epochs.get(team_id, 0) + 1
            session = self._sessions.get(team_id)
        if session is not None:
            try:
                self.cancel(team_id, session.id)
            except SignInSessionError:
                # The Team's Browser is removed and proven absent next, whatever this removal did.
                return

    def end_all(self) -> None:
        """A Space reset: every sign-in session ends at once, keeping nothing, and none still starting runs."""
        with self._guard:
            self._epoch += 1
            teams = list(self._sessions)
        for team_id in teams:
            self.end_team(team_id)

    def _release(self, lease: Lease, *, background: bool = False) -> bool:
        """Remove the session's Browser; whether it was proven gone.

        A failure leaves the runtime to clean every Browser before the next one starts, and meanwhile it is cleaned
        again in the background until its absence is proven, so the person's signed-in Browser never outlives its
        session for longer than Docker refuses to remove it.
        """
        try:
            self._runtime.release(lease)
        except BrowserError:
            _audit(lease, "release-failed", background=background)
            threading.Thread(target=self._recover, args=(lease,), name="browser-sign-in-recover", daemon=True).start()
            return False
        return True

    def _recover(self, lease: Lease) -> None:
        """Clean again until no residue is proven left; a restarted Team process cleans up after a stopped one."""
        while True:
            time.sleep(RECOVER_SECONDS)
            if self._runtime.recover():
                _audit(lease, "recovered", background=True)
                return


def _audit(lease: Lease, state: str, *, background: bool = False) -> None:
    """A sign-in session's states, never a URL path, page content, or input; Team's own when no request made them."""
    detail = f"{state}:container={lease.container_id[:12]}"
    if background:
        local_audit.record(
            "browser-sign-in", result="ok", principal=local_audit.TEAM, team_id=lease.team_id, detail=detail
        )
    else:
        local_audit.record_request("browser-sign-in", result="ok", team_id=lease.team_id, detail=detail)


def _activity(
    team_id: str,
    operation: str,
    phase: str,
    event_id: str,
    *,
    background: bool = False,
    flush: bool = False,
    **details: object,
) -> bool:
    """Record one activity event of the Team's Sign-in (ADR-0120); whether it was recorded, durably when `flush`.

    The actor is the request's verified principal, or Team itself on a background thread; never a cookie, input, or URL.
    """
    activity = {"event_id": event_id, "phase": phase, "subject": "sign-in", **details}
    try:
        principal = local_audit.TEAM if background else local_audit.request_principal()
        local_audit.record_activity(
            operation,
            result="error" if phase == "failed" else "ok",
            principal=principal,
            team_id=team_id,
            activity=activity,
        )
        if flush:
            local_audit.flush()
    except RuntimeError:
        return False
    return True
