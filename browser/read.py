"""A `read` step: Team opens up to ten links of the current page in turn (ADR-0117, 2026-10-22 amendment §1).

The Driver exposes no link destination, so a target is the link at its position in the page's ordered list of offered
link texts, re-resolved to the fresh ref at that position on every return. The batch goes on only while the returned
page has the same URL and the same ordered list; otherwise the remaining targets are not attempted. This is a heuristic
on public signed-out pages, not identity: each record carries the URL and title actually opened, and a click whose page
never changed is reported as not opened. Before every Driver command the caller proves the task may still act. Each
record is kept as soon as it is captured, so a failure later in the batch loses none, and the records join the next
observation within its bound.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import replace

from browser import cua
from protocol.http.v1 import browser as browser_protocol

READ_PAGE_CHARS = 2_000
MAX_SECTION_BYTES = 12 * 1024
# A click is read once its page's URL differs from the page it left, polled within this bound.
SETTLE_SECONDS = 0.3
SETTLE_TRIES = 10
HEADING = "\nPages you read last step:\n"
_REF_LINE = re.compile(r"(p[0-9]{1,6}:[0-9]{1,6}) ")


class UnstartedError(cua.CuaError):
    """The Driver failed before the read's first click: the batch had no effect on the page."""


def targets(decision: Mapping[str, object], observation: cua.Observation) -> list[int]:
    """The positions, among the page's offered links, of the refs a read names; refs that are no offered link drop."""
    positions = {ref: index for index, (ref, _name) in enumerate(observation.links)}
    return [positions[ref] for ref in decision["refs"] if ref in positions]


def run(
    session: cua.CuaSession,
    decision: Mapping[str, object],
    observation: cua.Observation,
    may_act: Callable[[], bool],
    sleep: Callable[[float], None],
    seen: Callable[[str, str], None],
    keep: Callable[[str], None],
) -> str:
    """The read's outcome; `keep` receives the section its pages add to the next observation as each one is read.

    A fatal Driver failure propagates, as UnstartedError when no link was clicked yet.
    """
    chosen = targets(decision, observation)
    if not chosen or not observation.url.startswith(("https://", "http://")):
        return "refused"
    names = [name for _ref, name in observation.links]
    records: list[str] = []
    clicked = False
    try:
        for position in chosen:
            if not may_act():
                break
            fresh = session.observe()
            if fresh.url != observation.url or [name for _ref, name in fresh.links] != names or not may_act():
                break
            clicked = True
            records.append(_open(session, fresh.links[position], observation.url, may_act, sleep, seen))
            keep(_section(records, len(chosen) - len(records)))
            if not may_act():
                break
            session.navigate(observation.url)
    except cua.CuaError as exc:
        if exc.fatal and not clicked:
            raise UnstartedError(exc.code) from exc
        raise
    return "ok" if records else "refused"


def _open(
    session: cua.CuaSession,
    link: tuple[str, str],
    source: str,
    may_act: Callable[[], bool],
    sleep: Callable[[float], None],
    seen: Callable[[str, str], None],
) -> str:
    try:
        session.click(link[0])
    except cua.CuaError as exc:
        if exc.fatal:
            raise
        return f"--- not loaded: {link[1]} ---"
    page = None
    for _try in range(SETTLE_TRIES):
        sleep(SETTLE_SECONDS)
        if not may_act():
            break
        page = session.observe()
        if page.url != source:
            break
    if page is None or page.url == source:
        return f"--- not opened: {link[1]} (the page did not change) ---"
    body = page.text.split("\npage:\n", 1)[-1][:READ_PAGE_CHARS]
    seen(page.url, body)
    return f"--- read {page.url} | {page.title} ---\n{body}"


def _section(records: list[str], left: int) -> str:
    section = HEADING + "\n".join(records)
    if left:
        section += f"\n({left} links not read: the page changed or the task could not go on)"
    return section.encode()[:MAX_SECTION_BYTES].decode(errors="ignore")


def shown(observation: cua.Observation, notice: str, section: str) -> cua.Observation:
    """The observation the agent sees: a notice before its text and a read section after, all within the bound.

    Only refs whose action line is still shown stay actionable, so a cut can never leave a hidden ref clickable.
    """
    room = browser_protocol.MAX_OBSERVATION_BYTES - len(section.encode())
    text = (notice + observation.text).encode()[: max(0, room)].decode(errors="ignore") + section
    visible = {
        match.group(1) for line in text.split("\npage:\n", 1)[0].splitlines() if (match := _REF_LINE.match(line))
    }
    refs = {ref: actions for ref, actions in observation.refs.items() if ref in visible}
    links = tuple(link for link in observation.links if link[0] in visible)
    return replace(observation, text=text, refs=refs, links=links)
