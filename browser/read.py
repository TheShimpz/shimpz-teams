"""A `read` step: Team opens up to ten links of the current page in turn (ADR-0117, 2026-10-22 amendment §1).

The Driver exposes no link destination, so a target is the link at its position in the page's ordered list of offered
link texts, re-resolved to the fresh ref at that position on every return. The batch goes on only while the returned
page has the same URL and the same ordered list; otherwise the remaining targets are not attempted. This is a heuristic
on public signed-out pages, not identity: each record carries the URL and title actually opened. Before each target the
caller proves the task may still act. The records join the next observation, within its bound.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from browser import cua
from protocol.http.v1 import browser as browser_protocol

READ_PAGE_CHARS = 2_000
MAX_SECTION_BYTES = 12 * 1024
SETTLE_SECONDS = 0.5
HEADING = "\nPages you read last step:\n"


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
) -> tuple[str, str]:
    """The read's outcome and the section its pages add to the next observation.

    A fatal Driver failure propagates: the batch's effect is then uncertain.
    """
    chosen = targets(decision, observation)
    if not chosen or not observation.url.startswith(("https://", "http://")):
        return "refused", ""
    names = [name for _ref, name in observation.links]
    records: list[str] = []
    for position in chosen:
        if not may_act():
            break
        fresh = session.observe()
        if fresh.url != observation.url or [name for _ref, name in fresh.links] != names:
            break
        records.append(_open(session, fresh.links[position], sleep, seen))
        session.navigate(observation.url)
    left = len(chosen) - len(records)
    if not records:
        return "refused", ""
    section = HEADING + "\n".join(records)
    if left:
        section += f"\n({left} links not read: the page changed or the task could not go on)"
    return "ok", section.encode()[:MAX_SECTION_BYTES].decode(errors="ignore")


def _open(
    session: cua.CuaSession, link: tuple[str, str], sleep: Callable[[float], None], seen: Callable[[str, str], None]
) -> str:
    try:
        session.click(link[0])
    except cua.CuaError as exc:
        if exc.fatal:
            raise
        return f"--- not loaded: {link[1]} ---"
    sleep(SETTLE_SECONDS)
    page = session.observe()
    body = page.text.split("\npage:\n", 1)[-1][:READ_PAGE_CHARS]
    seen(page.url, body)
    return f"--- read {page.url[: browser_protocol.MAX_URL_CHARS]} | {page.title} ---\n{body}"


def joined(text: str, section: str) -> str:
    """An observation's text with a read section after it, the text cut so both fit the observation bound."""
    if not section:
        return text
    room = browser_protocol.MAX_OBSERVATION_BYTES - len(section.encode())
    return text.encode()[: max(0, room)].decode(errors="ignore") + section
