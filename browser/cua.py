"""Team's MCP session with the Cua Driver inside one Browser (ADR-0117 §3.5).

Team is the only MCP client. It speaks newline-delimited JSON-RPC over the channel its Local profile opens (a
`docker exec` of `browser-host mcp`), calls only the closed set of tools below with arguments it builds itself, and
bounds every answer before anything reaches Brain. Page content is untrusted: it is returned as bounded text, never
interpreted.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from protocol.http.v1 import browser as browser_protocol

PROTOCOL_VERSION = "2025-06-18"
# The one lifecycle session label every call of a task carries; refs belong to it.
SESSION = "task"
TOOLS = frozenset(
    {
        "list_windows",
        "browser_prepare",
        "get_browser_state",
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_pointer",
    }
)
MAX_MESSAGE_BYTES = 4 * 1024 * 1024
MAX_OBSERVATION_BYTES = 24 * 1024
MAX_SCREENSHOT_BYTES = 2 * 1024 * 1024
MAX_REFS = 200
MAX_NAME_CHARS = 120
CALL_SECONDS = 30.0
# A tab mid-navigation is briefly reported with no tab marked active. Binding checks for the transition to one active
# tab at this interval, for as long as one call may take (never past the task's deadline); a tab that never becomes
# active in that time means the Browser hung, and it counts as lost.
BIND_POLL_SECONDS = 0.1
SCROLL_PIXELS = 600
# The Driver scrolls at a point: the middle of the Browser's 1280x800 kiosk window, which is all page.
SCROLL_AT = {"x": 640, "y": 400}
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# A page action the Driver explicitly refused leaves the Browser usable and the agent may try another; any other
# failure, an error that names no refusal included, may have had an effect and ends the task.
ACTIONS = frozenset({"browser_navigate", "browser_click", "browser_type", "browser_pointer"})
# A navigation the Driver reports as a Chromium network error left no page loaded but the Browser sound; the agent may
# look again and retry or go another way. The page may still have changed, so it is its own outcome, not a refusal.
NETWORK_FAILURE = re.compile(r"navigation failed: net::ERR_[A-Z0-9_]{1,64}")
# The Driver's grant on the Browser's window lapses with use (measured about 340 seconds after it was given) and says
# so with one refusal that asks for the same preparation again. Team renews it on its own admitted window, never with
# the Driver's suggested arguments: before binding once the grant is this old, and whenever the Driver says it lapsed.
REGRANT_SECONDS = 240.0
LAPSED_CODE = "browser_consent_required"
LAPSED_REASON = "consumer_profile_endpoint_requires_grant"


class Channel(Protocol):
    """A byte channel to the Cua Driver's stdio: whole writes, and one bounded line before an absolute deadline.

    Either raises OSError when the channel failed, closed, or missed the deadline.
    """

    def send(self, data: bytes, deadline: float) -> None: ...

    def receive_line(self, deadline: float, limit: int) -> bytes: ...


class CuaError(RuntimeError):
    """A closed failure of the session: the Browser cannot be trusted to continue, or one action was refused."""

    def __init__(self, code: str, *, fatal: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.fatal = fatal


class GrantLapsedError(CuaError):
    """The Driver's grant on the window lapsed: the call had no effect, and a renewal lets the session go on."""

    def __init__(self) -> None:
        super().__init__("browser-grant-lapsed")


@dataclass(frozen=True, slots=True)
class Observation:
    """The bounded page state Brain sees: its text, the refs each action may target, and an optional screenshot."""

    url: str
    title: str
    text: str
    refs: dict[str, frozenset[str]]
    screenshot: bytes | None = None
    # The offered links, in page order: each ref with role `link` offered for click, and its name.
    links: tuple[tuple[str, str], ...] = ()


def _clean(value: object, limit: int) -> str:
    """Page-supplied text as one bounded line without control characters."""
    if not isinstance(value, str):
        return ""
    text = "".join(character if character.isprintable() else " " for character in value)
    return " ".join(text.split())[:limit]


class CuaSession:
    """One task's session: initialize, bind Chromium's one window exactly, then observe and act on its tab."""

    def __init__(
        self,
        channel: Channel,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._channel = channel
        self._clock = clock
        self._sleep = sleep
        # The task's absolute deadline, which no call outlives; set by the task loop.
        self.deadline: float | None = None
        self._identifier = 0
        self._window: dict[str, object] | None = None
        self._target: tuple[str, str] | None = None
        # When the Driver last granted the window, by this session's clock.
        self._granted: float | None = None

    def initialize(self) -> None:
        """The MCP handshake; nothing is bound yet."""
        client = {"name": "shimpz-team", "version": "1"}
        self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": client})
        initialized = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n"
        try:
            self._channel.send(initialized, self._call_deadline())
        except OSError as exc:
            raise CuaError("channel-lost") from exc

    def chromium_windows(self) -> int:
        """How many top-level Chromium windows the display shows (the Driver's own cursor overlay is not one)."""
        windows = self._call("list_windows", {}).get("windows")
        if not isinstance(windows, list) or not all(isinstance(window, dict) for window in windows):
            raise CuaError("browser-window-unavailable")
        return sum(str(window.get("app_name", "")).startswith("Chromium") for window in windows)

    def open(self) -> None:
        self.initialize()
        windows = self._call("list_windows", {}).get("windows")
        if not isinstance(windows, list) or len(windows) != 1 or not isinstance(windows[0], dict):
            raise CuaError("browser-window-unavailable")
        window = windows[0]
        pid, window_id = window.get("pid"), window.get("window_id")
        chromium = str(window.get("app_name", "")).startswith("Chromium")
        if type(pid) is not int or type(window_id) is not int or not chromium:
            raise CuaError("browser-window-unavailable")
        self._window = {"pid": pid, "window_id": window_id, "session": SESSION}
        self._prepare()
        self._bind()

    def _prepare(self, within: float | None = None) -> None:
        """Attach the Driver to the admitted window, or renew its lapsed grant there; the same call either way."""
        if self._window is None:
            raise CuaError("browser-not-open")
        arguments = {**self._window, "strategy": {"kind": "existing_profile"}}
        if self._call("browser_prepare", arguments, within=within).get("status") != "ok":
            raise CuaError("browser-attach-refused")
        self._granted = self._clock()

    def _bind(self) -> None:
        """Bind the window and follow its active tab, so a link that opened a new tab is where the task continues.

        An old grant is renewed first, and a grant the Driver says lapsed is renewed once, within the same bound.
        """
        if self._window is None:
            raise CuaError("browser-not-open")
        # One bound for the whole binding: a renewal, every check, and the wait between checks end by it.
        settle = self._call_deadline()
        if self._granted is None or self._clock() - self._granted >= REGRANT_SECONDS:
            self._prepare(within=settle)
        renewed = False
        while True:
            try:
                target = self._bound(settle)
            except GrantLapsedError:
                if renewed:
                    raise CuaError("browser-binding-inexact") from None
                self._prepare(within=settle)
                renewed = True
                continue
            if target is not None:
                break
            if self._clock() + BIND_POLL_SECONDS > settle:
                raise CuaError("browser-binding-inexact")
            self._sleep(BIND_POLL_SECONDS)
        self._target = target

    def _bound(self, settle: float) -> tuple[str, str] | None:
        """The exact binding and its one active tab, or None while no tab is marked active yet."""
        bound = self._call("get_browser_state", dict(self._window or {}), within=settle)
        tabs = bound.get("tabs")
        target = bound.get("target_id")
        if bound.get("binding_quality") != "exact" or not isinstance(target, str) or not isinstance(tabs, list):
            raise CuaError("browser-binding-inexact")
        # Every tab marked active counts, so a malformed one never hides a second.
        active = [tab.get("tab_id") for tab in tabs if isinstance(tab, dict) and tab.get("active") is True]
        if len(active) > 1 or any(not isinstance(tab_id, str) for tab_id in active):
            raise CuaError("browser-binding-inexact")
        return (target, active[0]) if active else None

    def _tab(self) -> dict[str, str]:
        if self._target is None:
            raise CuaError("browser-not-open")
        return {"session": SESSION, "target_id": self._target[0], "tab_id": self._target[1]}

    def observe(self, *, screenshot: bool = False) -> Observation:
        self._bind()
        try:
            state = self._snapshot(screenshot)
        except GrantLapsedError:
            # The grant lapsed between binding and capture: renewed, the tab is bound again and captured once more.
            self._prepare()
            self._bind()
            try:
                state = self._snapshot(screenshot)
            except GrantLapsedError:
                raise CuaError("browser-binding-inexact") from None
        page = state.get("page") if isinstance(state.get("page"), dict) else {}
        url = _clean(page.get("url"), browser_protocol.MAX_URL_CHARS)
        title = _clean(page.get("title"), browser_protocol.MAX_TITLE_CHARS)
        head = f"url: {url}\ntitle: {title}\nactions:\n"
        refs, lines, links = _refs(state.get("refs"), MAX_OBSERVATION_BYTES - len(head.encode()) - len(b"\npage:\n"))
        outline = state.get("outline") if isinstance(state.get("outline"), str) else ""
        text = head + "\n".join(lines) + "\npage:\n"
        room = MAX_OBSERVATION_BYTES - len(text.encode())
        text += outline.encode()[:room].decode(errors="ignore")
        return Observation(url, title, text, refs, state.get("_screenshot") if screenshot else None, links)

    def navigate(self, url: str) -> None:
        self._act("browser_navigate", {**self._tab(), "url": url})

    def click(self, ref: str) -> None:
        # The container's one window is nobody else's, so trusted input may bring it to the front (ADR-0117).
        self._act("browser_click", {**self._tab(), "ref": ref, "delivery_mode": "foreground"})

    def type(self, ref: str, text: str) -> None:
        self._act("browser_type", {**self._tab(), "ref": ref, "text": text, "mode": "insert_text", "replace": True})

    def scroll(self, direction: str) -> None:
        delta = {"down": SCROLL_PIXELS, "up": -SCROLL_PIXELS}[direction]
        scroll = {"action": "scroll", "delta_y": delta, **SCROLL_AT, "delivery_mode": "foreground"}
        self._act("browser_pointer", {**self._tab(), **scroll})

    def _snapshot(self, screenshot: bool) -> dict[str, object]:
        return self._call(
            "get_browser_state",
            {**self._tab(), "snapshot_format": "semantic_v2", "include_screenshot": screenshot},
            images=screenshot,
        )

    def _act(self, tool: str, arguments: dict[str, object]) -> None:
        try:
            result = self._call(tool, arguments)
        except GrantLapsedError:
            # Refused with no effect: the grant is renewed, and the action is never repeated on refs it invalidated.
            self._prepare()
            raise CuaError("action-refused", fatal=False) from None
        if _refused(result):
            raise CuaError("action-refused", fatal=False)

    def _call(
        self, tool: str, arguments: dict[str, object], *, images: bool = False, within: float | None = None
    ) -> dict[str, object]:
        if tool not in TOOLS:
            raise CuaError("tool-not-admitted")
        result = self._request("tools/call", {"name": tool, "arguments": arguments}, within)
        structured = result.get("structuredContent")
        if _lapsed(structured):
            raise GrantLapsedError
        if result.get("isError") is True or not isinstance(structured, dict):
            if tool in ACTIONS and isinstance(structured, dict) and _refused(structured):
                raise CuaError("action-refused", fatal=False)
            if tool == "browser_navigate" and _network_failure(result):
                raise CuaError("navigation-failed", fatal=False)
            raise CuaError("tool-failed")
        if images:
            structured = {**structured, "_screenshot": _screenshot(result.get("content"))}
        return structured

    def _request(self, method: str, params: dict[str, object], within: float | None = None) -> dict[str, object]:
        self._identifier += 1
        identifier = self._identifier
        message = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        deadline = self._call_deadline(within)
        try:
            self._channel.send(json.dumps(message, separators=(",", ":")).encode() + b"\n", deadline)
            return self._answer(identifier, deadline)
        except OSError as exc:
            raise CuaError("channel-lost") from exc

    def _call_deadline(self, within: float | None = None) -> float:
        """When the next call must have ended: its own bound, never past the task's or `within`; none if passed."""
        deadline = self._clock() + CALL_SECONDS
        for bound in (self.deadline, within):
            if bound is not None:
                deadline = min(deadline, bound)
        if deadline <= self._clock():
            raise CuaError("deadline-passed")
        return deadline

    def _answer(self, identifier: int, deadline: float) -> dict[str, object]:
        while True:
            line = self._channel.receive_line(deadline, MAX_MESSAGE_BYTES)
            try:
                answer = json.loads(line)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CuaError("protocol-invalid") from exc
            if not isinstance(answer, dict):
                raise CuaError("protocol-invalid")
            if "id" not in answer:
                continue  # a notification the session does not use
            if answer.get("id") != identifier:
                raise CuaError("protocol-invalid")
            if "error" in answer or not isinstance(answer.get("result"), dict):
                raise CuaError("protocol-error")
            return answer["result"]


def _network_failure(result: dict[str, object]) -> bool:
    """Whether the Driver says only that the page did not load for a network error, the Browser itself still sound.

    The text is matched, never forwarded: the agent learns only that the navigation failed.
    """
    content = result.get("content")
    if result.get("isError") is not True or not isinstance(content, list) or len(content) != 1:
        return False
    item = content[0]
    return (
        isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
        and NETWORK_FAILURE.fullmatch(item["text"]) is not None
    )


def _lapsed(structured: object) -> bool:
    """Whether the Driver refused because its grant on the window lapsed, and for nothing else."""
    if not isinstance(structured, dict) or structured.get("status") != "refused":
        return False
    refusal = structured.get("refusal")
    detail = refusal.get("detail") if isinstance(refusal, dict) else None
    return (
        isinstance(refusal, dict)
        and refusal.get("code") == LAPSED_CODE
        and isinstance(detail, dict)
        and detail.get("reason") == LAPSED_REASON
    )


def _refused(structured: dict[str, object]) -> bool:
    """Whether the Driver's answer names an explicit refusal of the action, which had no effect."""
    return structured.get("effect") == "refused" or structured.get("status") == "refused"


def _refs(entries: object, budget: int) -> tuple[dict[str, frozenset[str]], list[str], tuple[tuple[str, str], ...]]:
    """The refs the observation offers, their lines, and its offered links, within `budget` UTF-8 bytes.

    Only a ref shown is offered; a ref outside the protocol's form is never offered, since no step could name it.
    """
    refs: dict[str, frozenset[str]] = {}
    lines: list[str] = []
    links: list[tuple[str, str]] = []
    for entry in entries if isinstance(entries, list) else ():
        if len(refs) >= MAX_REFS or not isinstance(entry, dict):
            break
        ref, actions = entry.get("ref"), entry.get("actions")
        if not isinstance(ref, str) or not browser_protocol.REF_RE.fullmatch(ref) or not isinstance(actions, list):
            continue
        admitted = frozenset(action for action in actions if action in {"click", "type", "scroll", "pointer"})
        if not admitted:
            continue
        role, name = _clean(entry.get("role"), 32), _clean(entry.get("name"), MAX_NAME_CHARS)
        line = f'{ref} {role} "{name}" ({", ".join(sorted(admitted))})'
        # Each line costs its bytes and the newline that joins it.
        cost = len(line.encode()) + 1
        if cost > budget:
            break
        budget -= cost
        refs[ref] = admitted
        lines.append(line)
        if role == "link" and "click" in admitted:
            links.append((ref, name))
    return refs, lines, tuple(links)


def _screenshot(content: object) -> bytes | None:
    """The one PNG image a snapshot carries, within its bound, or None."""
    for item in content if isinstance(content, list) else ():
        if isinstance(item, dict) and item.get("type") == "image" and item.get("mimeType") == "image/png":
            data = item.get("data")
            if not isinstance(data, str) or len(data) > MAX_SCREENSHOT_BYTES * 4 // 3 + 4:
                return None
            try:
                image = base64.b64decode(data, validate=True)
            except binascii.Error, ValueError:
                return None
            return image if image.startswith(PNG_SIGNATURE) and len(image) <= MAX_SCREENSHOT_BYTES else None
    return None
