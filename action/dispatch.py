"""Admission of Action RPC Docker calls to one shared bounded pool, and Team's own refusal before dispatch.

Every Docker call of an Action RPC runs on the pool. A call that outlives its budget keeps its slot until Docker answers
or the client's own timeout ends it, so abandoned calls never accumulate; a wait for a slot observes the turn's Stop,
and a dispatch Team refuses before its workload process starts is settled as never run (ADR-0092, ADR-0093).
"""

from __future__ import annotations

import concurrent.futures
import contextvars
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager


class DispatchRefusedError(RuntimeError):
    """Team refused an RPC before its workload process was started.

    Its deadline passed or Docker capacity stayed saturated. Nothing ran, so the attempt is settled as never dispatched
    rather than left uncertain.
    """


def never_dispatched(exc: BaseException | None) -> bool:
    """Whether a failed attempt was refused by Team before its workload process started.

    Only the setup refusal is chained to a ``DispatchRefusedError``; a refusal after the exchange started, such as an
    exit inspection that found no capacity, is raised without it and stays uncertain.
    """
    for _depth in range(8):
        if exc is None:
            return False
        if isinstance(exc, DispatchRefusedError):
            return True
        exc = exc.__cause__
    return False


# Once every slot is held, a new RPC is refused before it starts anything.
MAX_DOCKER_CALLS = 8
_DOCKER_CALLS = concurrent.futures.ThreadPoolExecutor(max_workers=MAX_DOCKER_CALLS, thread_name_prefix="action-docker")
_DOCKER_CALL_SLOTS = threading.BoundedSemaphore(MAX_DOCKER_CALLS)


_SLOT_POLL_SECONDS = 0.25


def _never() -> bool:
    return False


# The turn's Stop, observed by an RPC while it waits for Docker capacity to dispatch its workload.
_STOPPED: contextvars.ContextVar[Callable[[], bool]] = contextvars.ContextVar("action_rpc_stopped", default=_never)


def current_stop() -> Callable[[], bool]:
    """The Stop of the turn whose RPC is dispatching now; a turn without one is never stopped."""
    return _STOPPED.get()


@contextmanager
def observing_stop(stopped: Callable[[], bool]) -> Iterator[None]:
    """Let every RPC this block dispatches observe the turn's Stop while it waits for Docker capacity."""
    token = _STOPPED.set(stopped)
    try:
        yield
    finally:
        _STOPPED.reset(token)


def bounded_call[T](
    call: Callable[[], T], deadline: float, stopped: Callable[[], bool] = _never
) -> concurrent.futures.Future[T]:
    """Run one Docker call on the shared pool, admitted only while a slot frees within the remaining budget.

    The wait is sliced so a stopped turn is refused promptly; Docker calls already running keep their slots.
    """
    while True:
        if stopped():
            raise DispatchRefusedError("the turn was stopped before its Docker call could run")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DispatchRefusedError("the Action deadline passed before its Docker call could run")
        if _DOCKER_CALL_SLOTS.acquire(timeout=min(_SLOT_POLL_SECONDS, remaining)):
            break
    if stopped():
        # Stop may win while the wait succeeds; the slot is returned before anything runs.
        _DOCKER_CALL_SLOTS.release()
        raise DispatchRefusedError("the turn was stopped before its Docker call could run")
    try:
        future = _DOCKER_CALLS.submit(call)
    except BaseException:
        _DOCKER_CALL_SLOTS.release()
        raise
    future.add_done_callback(lambda _done: _DOCKER_CALL_SLOTS.release())
    return future
