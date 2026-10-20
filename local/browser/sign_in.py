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

A session that began starting before its Team was deleted, its Sign-in deleted, or the Space reset is never registered:
each end bumps an epoch that a starting session must still see unchanged once its Browser exists.

A Supervisor's deletion is `deleted` only once the Sign-in's capsules are proven gone, no session of the Team is running
or starting, its Browser's removal was proven, and its completion is recorded; until then it is `pending` and Team keeps
trying, across a restart too. Every step records its activity event (ADR-0120): starting and Saving delegate the
Sign-in, so they refuse when the journal cannot record them; Cancel and deletion protect it, so they never wait on it.
"""

from __future__ import annotations

import contextlib
import logging
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
START, SAVE, CANCEL, DELETE, EXPIRE = (
    "browser-sign-in-start",
    "browser-sign-in-save",
    "browser-sign-in-cancel",
    "browser-sign-in-delete",
    "browser-sign-in-expire",
)
# The origin of what Team does on its own, beside the verified Admin Origin of what a person asked (ADR-0120).
TEAM_ORIGIN = "team"
log = logging.getLogger("shimpz.team.local.browser.sign_in")

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
    # The verified person who started it, and the Origin they started it from, which Team's own later events name.
    initiator: str = ""
    origin: str = ""


@dataclass(frozen=True, slots=True)
class _Deletion:
    """A Supervisor's deletion of a Team's Sign-in, as its activity events name it until its completion is recorded."""

    team_id: str
    event_id: str
    generation: int
    incarnation: str
    factor: str
    initiator: str
    origin: str

    def details(self) -> dict[str, object]:
        return {"generation": self.generation, "incarnation": self.incarnation, "factor": self.factor}


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
        # Teams with a sign-in session starting, and Teams whose sign-in Browser's removal is not proven yet.
        self._starting: set[str] = set()
        # Each residue is one Browser, by its Team and container, so a removal proven later clears only its own.
        self._residue: set[tuple[str, str]] = set()
        # Cleared while the Browsers a previous Team process left are not proven gone; no deletion completes meanwhile.
        self._cleaned = threading.Event()
        self._cleaned.set()
        store.expired = self._expired

    def start(
        self, team_id: str, incarnation: Incarnation, url: object, locale: str | None, origin: str
    ) -> dict[str, str]:
        """Open a sign-in session for the Team at `url`; its id and the view the person controls it through."""
        site = sign_in_protocol.start_site(url)
        if site is None:
            raise SignInSessionError(sign_in_protocol.URL_INVALID)
        with self._guard:
            if team_id in self._starting:
                raise SignInSessionError(sign_in_protocol.BUSY)
            self._starting.add(team_id)
            admission = self._epoch, self._epochs.get(team_id, 0)
        try:
            return self._start(team_id, incarnation, url, locale, origin, site, admission)
        finally:
            with self._guard:
                self._starting.discard(team_id)

    def _start(
        self,
        team_id: str,
        incarnation: Incarnation,
        url: object,
        locale: str | None,
        origin: str,
        site: str,
        admission: tuple,
    ) -> dict[str, str]:
        current = incarnation()
        if current is None:
            raise SignInSessionError(TEAM_NOT_FOUND)
        event, initiator = secrets.token_hex(16), local_audit.human_principal() or ""
        # Starting delegates the Team's Sign-in to a person: nothing starts unless that is durably recorded first.
        if not _activity(team_id, START, "requested", event, origin=origin, flush=True, incarnation=current):
            raise SignInSessionError(UNAVAILABLE)
        checkout = self._checkout(team_id, current, event, origin)
        details = {"generation": checkout.generation, "incarnation": current, "origin": origin}
        try:
            capsule = self._store.capsule(checkout, self._runtime.image or "")
            lease = self._runtime.acquire(team_id, language(locale), mode=PERSON, url=str(url), capsule=capsule)
        except (BrowserError, sign_in_store.SignInError) as exc:
            self._store.finish(checkout)
            _activity(team_id, START, "failed", event, outcome=exc.code or UNAVAILABLE, **details)
            raise SignInSessionError(exc.code or UNAVAILABLE) from exc
        session = self._session(team_id, current, site, lease, checkout, (initiator, origin))
        try:
            self._service.hold(team_id, f"sign-in:{session.id}", lease, session.view)
        except BrowserError as exc:
            self._release(lease)
            self._store.finish(checkout)
            _activity(team_id, START, "failed", event, outcome=exc.code, **details)
            raise SignInSessionError(exc.code) from exc
        with self._guard:
            admitted = (self._epoch, self._epochs.get(team_id, 0)) == admission
            if admitted:
                self._sessions[team_id] = session
        if not admitted or incarnation() != current:
            # The Team or its Sign-in was deleted, or the Space reset, while this Browser started: it never becomes a
            # session. Its removal has one owner: this start, unless a Cancel or the Team's end already took it.
            with self._guard:
                owned = not admitted or self._sessions.get(team_id) is session
                if admitted and owned:
                    del self._sessions[team_id]
                if owned:
                    self._residue.add((team_id, lease.container_id))
            if owned:
                session.ended.set()
                self._fence(session)
                self._release(session.lease)
                self._store.finish(checkout)
            _activity(team_id, START, "failed", event, outcome=sign_in_protocol.NOT_RUNNING, **details)
            raise SignInSessionError(sign_in_protocol.NOT_RUNNING)
        # The session's limits hold from here, whatever happens to its answer.
        threading.Thread(target=self._watch, args=(session,), name="browser-sign-in", daemon=True).start()
        if not _activity(team_id, START, "completed", event, flush=True, **details):
            # A delegation the journal could not record does not stand: the session ends at once, keeping nothing.
            with contextlib.suppress(SignInSessionError):
                self.cancel(team_id, session.id, reason="unrecorded")
            raise SignInSessionError(UNAVAILABLE)
        _audit(lease, f"started:site={site}")
        return {"sign_in_session_id": session.id, "view_id": session.view.id, "site": site}

    def _session(
        self,
        team_id: str,
        incarnation: str,
        site: str,
        lease: Lease,
        checkout: sign_in_store.Checkout,
        started_by: tuple[str, str],
    ) -> _Session:
        credentials = lease.credentials
        view = local_view.View(
            lambda: self._runtime.open_relay(lease),
            lambda exec_id, deadline: self._runtime.exited(exec_id, deadline),
            lambda state: _audit(lease, f"view:{state}"),
            authorization=credentials.authorization if credentials is not None else None,
            private=True,
        )
        initiator, origin = started_by
        return _Session(
            secrets.token_hex(16),
            team_id,
            incarnation,
            site,
            lease,
            view,
            self._clock(),
            checkout,
            initiator=initiator,
            origin=origin,
        )

    def _checkout(self, team_id: str, incarnation: str, event: str, origin: str) -> sign_in_store.Checkout:
        try:
            return self._store.checkout(team_id, incarnation, sign_in_store.PERSON)
        except sign_in_store.SignInError as exc:
            code = exc.code or UNAVAILABLE
            _activity(team_id, START, "failed", event, origin=origin, incarnation=incarnation, outcome=code)
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
            # Until its release proves the Browser gone, no deletion of the Sign-in it restored is complete.
            self._residue.add((team_id, session.lease.container_id))
        session.ended.set()
        self._fence(session)
        return session, False

    def cancel(
        self,
        team_id: str,
        session_id: object,
        *,
        origin: str = "",
        background: bool = False,
        reason: str = "cancelled",
    ) -> None:
        """End the session, keeping nothing, once its Browser was proven removed; else it is refused as unavailable.

        A Save under way removes the Browser itself: Cancel answers once it did. A removal that failed leaves the
        Space's Browser to be cleaned and proven absent before the next one starts. Its activity event names why it
        ended (`reason`) and fails when the removal was not proven; without its own Origin, it names the session's.
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
            CANCEL,
            "completed" if removed else "failed",
            session.id,
            origin=origin or session.origin,
            own=session.initiator if background else None,
            generation=session.checkout.generation,
            incarnation=session.incarnation,
            outcome=reason,
        )
        if not removed:
            raise SignInSessionError(UNAVAILABLE)

    def save(self, team_id: str, session_id: object, incarnation: Incarnation, origin: str) -> dict[str, object]:
        """Keep the session's profile as the Team's Sign-in's next generation; the Browser is released however it ends.

        Saving delegates the logins it holds, so nothing is saved unless that is durably recorded first. A completion
        that could not be recorded after the commit is answered as committed, with `recorded` false (ADR-0120).
        """
        session = self._running(team_id, session_id)
        if self._windows(session.lease) != 1:
            raise SignInSessionError(sign_in_protocol.WINDOWS)
        details = {"generation": session.checkout.generation, "incarnation": session.incarnation, "origin": origin}
        if not _activity(team_id, SAVE, "requested", session.id, flush=True, **details):
            raise SignInSessionError(UNAVAILABLE)
        with self._guard:
            if self._sessions.get(team_id) is not session or session.saving or session.ended.is_set():
                raise SignInSessionError(sign_in_protocol.NOT_RUNNING)
            session.saving = True
        saved, refusal = None, UNAVAILABLE
        try:
            saved = self._persist(session, incarnation)
        except SignInSessionError as exc:
            refusal = exc.code
            raise
        finally:
            # Nothing else takes a saving session away: it leaves here, ended.
            with self._guard:
                del self._sessions[team_id]
                self._residue.add((team_id, session.lease.container_id))
            session.ended.set()
            self._fence(session)
            session.removed = self._release(session.lease)
            if saved is None:
                self._store.finish(session.checkout)
                _activity(team_id, SAVE, "failed", session.id, outcome=refusal, **details)
            session.released.set()
        details["generation"] = int(saved["generation"])
        recorded = _activity(team_id, SAVE, "completed", session.id, flush=True, outcome="saved", **details)
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

        Team re-checks the confirmed Sign-in, generation, and incarnation, fences the Sign-in durably with the
        deletion's metadata, ends a session using it or starting, and removes everything: `deleted` once proven and
        recorded, else `pending` while it keeps trying in the background.
        """
        deletion = _Deletion(
            team_id,
            secrets.token_hex(16),
            int(confirmed["generation"]),
            str(confirmed["team_incarnation"]),
            str(confirmed["factor"]),
            local_audit.human_principal() or "",
            str(confirmed["origin"]),
        )
        # Deleting is protective: an activity journal that cannot record it never stops it (ADR-0120).
        _activity(team_id, DELETE, "requested", deletion.event_id, origin=deletion.origin, **deletion.details())
        metadata = {
            "event_id": deletion.event_id,
            "initiator_id": deletion.initiator,
            "factor": deletion.factor,
            "origin": deletion.origin,
        }
        expected = (str(confirmed["sign_in_id"]), deletion.generation, deletion.incarnation)
        try:
            self._store.begin_delete(team_id, expected, metadata)
        except sign_in_store.SignInError as exc:
            code = exc.code or UNAVAILABLE
            _activity(
                team_id, DELETE, "failed", deletion.event_id, origin=deletion.origin, outcome=code, **deletion.details()
            )
            raise SignInSessionError(code) from exc
        with self._guard:
            # A session still starting never becomes one; one running ends now, and the fence keeps its Save out.
            self._epochs[team_id] = self._epochs.get(team_id, 0) + 1
            session = self._sessions.get(team_id)
        if session is not None:
            with contextlib.suppress(SignInSessionError):
                self.cancel(team_id, session.id, origin=deletion.origin, reason="deleted")
        if self._settle(deletion, background=False):
            return "deleted"
        self._later(deletion)
        return "pending"

    def _settle(self, deletion: _Deletion, *, background: bool) -> bool:
        """Complete the deletion if it can be proven now: whether its completion was recorded and its fence dropped."""
        team_id = deletion.team_id
        with self._guard:
            residue = not self._cleaned.is_set() or any(owner == team_id for owner, _container in self._residue)
            if team_id in self._starting or team_id in self._sessions or residue:
                return False
        try:
            if not self._store.purge(team_id, deletion.event_id):
                return False
        except sign_in_store.SignInError:
            return False
        if not _activity(
            team_id,
            DELETE,
            "completed",
            deletion.event_id,
            origin=deletion.origin,
            flush=True,
            own=deletion.initiator if background else None,
            outcome="deleted",
            **deletion.details(),
        ):
            return False
        try:
            return self._store.settle(team_id, deletion.event_id)
        except sign_in_store.SignInError:
            return False

    def _later(self, deletion: _Deletion) -> None:
        name = "browser-sign-in-delete"
        threading.Thread(target=self._delete_later, args=(deletion,), name=name, daemon=True).start()

    def _delete_later(self, deletion: _Deletion) -> None:
        """Retry a fenced deletion until it is proven and recorded; a restarted Team process resumes it at its start."""
        while True:
            time.sleep(RECOVER_SECONDS)
            if self._settle(deletion, background=True):
                return

    def current(self, team_id: str) -> dict[str, object] | None:
        """The Team's Sign-in as Admin shows it: needing a person when this Team's Browser image cannot restore it."""
        return self._store.current(team_id, self._runtime.image)

    def startup(self) -> None:
        """At Team start, after the store reconciled: prove no Browser is left, then resume every pending deletion.

        A Browser a previous Team process left is removed first (ADR-0117 §8); until that is proven, no deletion of a
        Sign-in it may have restored is reported complete. Pending deletions the store cannot read yet are read again
        until it can.
        """
        # Cleanup never waits on the Sign-ins' metadata: a previous process's Browser is removed first, whatever it is,
        # and a cleanup that failed is tried again until it is proven, whether or not a deletion waits on it.
        if not self._service.startup():
            self._cleaned.clear()
            name = "browser-sign-in-recover"
            threading.Thread(target=self._recover_startup, name=name, daemon=True).start()
        pending = self._pending()
        for deletion in pending:
            if not self._settle(deletion, background=True):
                self._later(deletion)

    def _pending(self) -> list[_Deletion]:
        while True:
            try:
                return [
                    _Deletion(
                        team_id,
                        str(meta["event_id"]),
                        int(meta["generation"]),
                        str(meta["incarnation"]),
                        str(meta["factor"]),
                        str(meta["initiator_id"]),
                        str(meta["origin"]),
                    )
                    for team_id, meta in self._store.deletions()
                ]
            except sign_in_store.SignInError as exc:
                log.warning("pending Sign-in deletions are unreadable, read again shortly: %s", exc)
                time.sleep(RECOVER_SECONDS)

    def _expired(self, team_id: str, generation: int, incarnation: str) -> None:
        """The store deleted the Team's expired Sign-in as it read it: Team records so on its own (ADR-0117 item 7)."""
        details = {"generation": generation, "incarnation": incarnation, "outcome": "expired"}
        _activity(team_id, EXPIRE, "completed", secrets.token_hex(16), origin=TEAM_ORIGIN, own="", **details)

    def _watch(self, session: _Session) -> None:
        """End the session at its limit, once the person left it alone too long, or once its screen is lost.

        A screen is lost when its Browser failed: a session no one can drive any more is never left running.
        """
        while not session.ended.wait(WATCH_SECONDS):
            limit = self._clock() - session.started >= SESSION_SECONDS
            reason = "lost" if session.view.unavailable else "session-limit" if limit else "idle"
            if reason != "idle" or session.view.quiet_for() >= IDLE_SECONDS:
                _audit(session.lease, "lost" if reason == "lost" else "expired", background=True)
                try:
                    self.cancel(session.team_id, session.id, background=True, reason=reason)
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
                self.cancel(team_id, session.id, reason="team-deleted")
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
            self._recovering(lease.team_id, lease)
            return False
        with self._guard:
            self._residue.discard((lease.team_id, lease.container_id))
        return True

    def _recovering(self, team_id: str, lease: Lease) -> None:
        """Clean `lease`'s Browser in the background until it is proven gone."""
        with self._guard:
            self._residue.add((team_id, lease.container_id))
        threading.Thread(
            target=self._recover, args=(team_id, lease), name="browser-sign-in-recover", daemon=True
        ).start()

    def _recover_startup(self) -> None:
        """Clean again until no Browser a previous Team process left is proven left."""
        while True:
            time.sleep(RECOVER_SECONDS)
            if self._runtime.recover():
                self._cleaned.set()
                return

    def _recover(self, team_id: str, lease: Lease) -> None:
        """Clean again until no residue is proven left; a restarted Team process cleans up after a stopped one."""
        while True:
            time.sleep(RECOVER_SECONDS)
            if self._runtime.recover():
                with self._guard:
                    self._residue.discard((team_id, lease.container_id))
                _audit(lease, "recovered", background=True)
                return


def _audit(lease: Lease, state: str, *, background: bool = False) -> None:
    """A sign-in session's states, never a URL path, page content, or input; Team's own when no request made them.

    A journal that cannot record one never stops what the session does (ADR-0120); its activity events decide that.
    """
    detail = f"{state}:container={lease.container_id[:12]}"
    try:
        if background:
            local_audit.record(
                "browser-sign-in", result="ok", principal=local_audit.TEAM, team_id=lease.team_id, detail=detail
            )
        else:
            local_audit.record_request("browser-sign-in", result="ok", team_id=lease.team_id, detail=detail)
    except RuntimeError:
        log.warning("the journal could not record a sign-in session's %s", state.partition(":")[0])


def _activity(
    team_id: str,
    operation: str,
    phase: str,
    event_id: str,
    *,
    origin: str,
    own: str | None = None,
    flush: bool = False,
    **details: object,
) -> bool:
    """Record one activity event of the Team's Sign-in (ADR-0120); whether it was recorded, durably when `flush`.

    The actor is the request's verified principal, and the origin the verified Admin Origin it came from. An event
    Team records on its own (`own`, on a background thread) has Team as its actor and origin, and names the verified
    person who began it as its initiator (`own`, empty when none). Never a cookie, input, or URL.
    """
    activity = {"event_id": event_id, "phase": phase, "subject": "sign-in", **details}
    activity["origin"] = origin if own is None else TEAM_ORIGIN
    if own:
        activity["initiator_id"] = own
    try:
        principal = local_audit.TEAM if own is not None else local_audit.request_principal()
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
