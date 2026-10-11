"""Bounded ephemeral reuse of staged Local snapshot admission verdicts (ADR-0060).

A verdict depends only on the immutable image and on this Team's contract, so a settled one, admissible or
inadmissible, is reused for its exact image id and current tag set; an unavailable one says nothing about the image
and is computed again on the next read.
"""

import threading
from collections import OrderedDict
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass

from local.install import snapshots

MAX_CACHED_VERDICTS = 2 * snapshots.MAX_CANDIDATES
MAX_CONCURRENT_EXTRACTIONS = 2

Judge = Callable[[object, str], snapshots.Admission]


@dataclass(frozen=True, slots=True)
class Judged:
    """One candidate's verdict, and whether this read settled it rather than reusing or joining another read's."""

    candidate: snapshots.LocalSnapshotCandidate
    admission: snapshots.Admission
    fresh: bool


def _key(candidate: snapshots.LocalSnapshotCandidate) -> tuple[str, tuple[str, ...]]:
    # A listed candidate carries exactly its Assistant's one current tag; the key names it so any retag judges anew.
    return candidate.image_id, (snapshots.canonical_reference(candidate.assistant_id),)


class LocalSnapshotAdmissions:
    """Judge every listed snapshot once per immutable image, with bounded extraction concurrency.

    Concurrent reads of one uncached image join a single judgment, and at most ``MAX_CONCURRENT_EXTRACTIONS``
    judgments extract image files at once across every read.
    """

    def __init__(self, client, *, judge: Judge = snapshots.admission) -> None:
        self._client = client
        self._judge = judge
        self._lock = threading.Lock()
        self._verdicts: OrderedDict[tuple[str, tuple[str, ...]], snapshots.Admission] = OrderedDict()
        self._judgments: dict[tuple[str, tuple[str, ...]], Future[snapshots.Admission]] = {}
        self._slots = threading.BoundedSemaphore(MAX_CONCURRENT_EXTRACTIONS)

    def judge(self, candidates: tuple[snapshots.LocalSnapshotCandidate, ...]) -> tuple[Judged, ...]:
        """Every candidate's verdict, in the candidates' order."""
        if not candidates:
            return ()
        with ThreadPoolExecutor(
            max_workers=min(MAX_CONCURRENT_EXTRACTIONS, len(candidates)),
            thread_name_prefix="local-snapshot-admission",
        ) as executor:
            futures = [executor.submit(self._judged, candidate) for candidate in candidates]
        return tuple(future.result() for future in futures)

    def _judged(self, candidate: snapshots.LocalSnapshotCandidate) -> Judged:
        key = _key(candidate)
        with self._lock:
            cached = self._verdicts.get(key)
            if cached is not None:
                self._verdicts.move_to_end(key)
                return Judged(candidate, cached, fresh=False)
            judgment = self._judgments.get(key)
            owned = judgment is None
            if owned:
                judgment = self._judgments[key] = Future()
        if not owned:
            return Judged(candidate, judgment.result(), fresh=False)
        return Judged(candidate, self._settle(key, candidate.image_id, judgment), fresh=True)

    def _settle(
        self,
        key: tuple[str, tuple[str, ...]],
        image_id: str,
        judgment: Future[snapshots.Admission],
    ) -> snapshots.Admission:
        """Judge in one bounded slot, remember a settled verdict, and hand the outcome to every joined read."""
        try:
            with self._slots:
                verdict = self._judge(self._client, image_id)
            if verdict.state != snapshots.UNAVAILABLE:
                self._remember(key, verdict)
        except BaseException as exc:
            judgment.set_exception(exc)
            raise
        finally:
            with self._lock:
                del self._judgments[key]
        judgment.set_result(verdict)
        return verdict

    def _remember(self, key: tuple[str, tuple[str, ...]], verdict: snapshots.Admission) -> None:
        with self._lock:
            self._verdicts[key] = verdict
            self._verdicts.move_to_end(key)
            while len(self._verdicts) > MAX_CACHED_VERDICTS:
                self._verdicts.popitem(last=False)
