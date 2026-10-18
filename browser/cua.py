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
SCROLL_PIXELS = 600
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
# A page action the Driver explicitly refused leaves the Browser usable and the agent may try another; any other
# failure, an error that names no refusal included, may have had an effect and ends the task.
ACTIONS = frozenset({"browser_navigate", "browser_click", "browser_type", "browser_pointer"})


class Channel(Protocol):
    """A byte channel to the Cua Driver's stdio: whole writes, and one bounded line before an absolute deadline.

    Either raises OSError when the channel failed, closed, or missed the deadline.
    """

    def send(self, data: bytes) -> None: ...

    def receive_line(self, deadline: float, limit: int) -> bytes: ...


class CuaError(RuntimeError):
    """A closed failure of the session: the Browser cannot be trusted to continue, or one action was refused."""

    def __init__(self, code: str, *, fatal: bool = True) -> None:
        super().__init__(code)
        self.code = code
        self.fatal = fatal


@dataclass(frozen=True, slots=True)
class Observation:
    """The bounded page state Brain sees: its text, the refs each action may target, and an optional screenshot."""

    url: str
    title: str
    text: str
    refs: dict[str, frozenset[str]]
    screenshot: bytes | None = None


def _clean(value: object, limit: int) -> str:
    """Page-supplied text as one bounded line without control characters."""
    if not isinstance(value, str):
        return ""
    text = "".join(character if character.isprintable() else " " for character in value)
    return " ".join(text.split())[:limit]


class CuaSession:
    """One task's session: initialize, bind Chromium's one window exactly, then observe and act on its tab."""

    def __init__(self, channel: Channel, clock: Callable[[], float] = time.monotonic) -> None:
        self._channel = channel
        self._clock = clock
        # The task's absolute deadline, which no call outlives; set by the task loop.
        self.deadline: float | None = None
        self._identifier = 0
        self._window: dict[str, object] | None = None
        self._target: tuple[str, str] | None = None

    def open(self) -> None:
        client = {"name": "shimpz-team", "version": "1"}
        self._request("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {}, "clientInfo": client})
        self._channel.send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}).encode() + b"\n")
        windows = self._call("list_windows", {}).get("windows")
        if not isinstance(windows, list) or len(windows) != 1 or not isinstance(windows[0], dict):
            raise CuaError("browser-window-unavailable")
        window = windows[0]
        pid, window_id = window.get("pid"), window.get("window_id")
        chromium = str(window.get("app_name", "")).startswith("Chromium")
        if type(pid) is not int or type(window_id) is not int or not chromium:
            raise CuaError("browser-window-unavailable")
        self._window = {"pid": pid, "window_id": window_id, "session": SESSION}
        prepared = self._call("browser_prepare", {**self._window, "strategy": {"kind": "existing_profile"}})
        if prepared.get("status") != "ok":
            raise CuaError("browser-attach-refused")
        self._bind()

    def _bind(self) -> None:
        """Bind the window and follow its active tab, so a link that opened a new tab is where the task continues."""
        if self._window is None:
            raise CuaError("browser-not-open")
        bound = self._call("get_browser_state", dict(self._window))
        tabs = bound.get("tabs")
        target = bound.get("target_id")
        if bound.get("binding_quality") != "exact" or not isinstance(target, str) or not isinstance(tabs, list):
            raise CuaError("browser-binding-inexact")
        active = [
            tab["tab_id"]
            for tab in tabs
            if isinstance(tab, dict) and tab.get("active") is True and isinstance(tab.get("tab_id"), str)
        ]
        if len(active) != 1:
            raise CuaError("browser-binding-inexact")
        self._target = (target, active[0])

    def _tab(self) -> dict[str, str]:
        if self._target is None:
            raise CuaError("browser-not-open")
        return {"session": SESSION, "target_id": self._target[0], "tab_id": self._target[1]}

    def observe(self, *, screenshot: bool = False) -> Observation:
        self._bind()
        state = self._call(
            "get_browser_state",
            {**self._tab(), "snapshot_format": "semantic_v2", "include_screenshot": screenshot},
            images=screenshot,
        )
        page = state.get("page") if isinstance(state.get("page"), dict) else {}
        url, title = _clean(page.get("url"), 2048), _clean(page.get("title"), 300)
        head = f"url: {url}\ntitle: {title}\nactions:\n"
        refs, lines = _refs(state.get("refs"), MAX_OBSERVATION_BYTES - len(head.encode()) - len(b"\npage:\n"))
        outline = state.get("outline") if isinstance(state.get("outline"), str) else ""
        text = head + "\n".join(lines) + "\npage:\n"
        room = MAX_OBSERVATION_BYTES - len(text.encode())
        text += outline.encode()[:room].decode(errors="ignore")
        return Observation(url, title, text, refs, state.get("_screenshot") if screenshot else None)

    def navigate(self, url: str) -> None:
        self._act("browser_navigate", {**self._tab(), "url": url})

    def click(self, ref: str) -> None:
        # The container's one window is nobody else's, so trusted input may bring it to the front (ADR-0117).
        self._act("browser_click", {**self._tab(), "ref": ref, "delivery_mode": "foreground"})

    def type(self, ref: str, text: str) -> None:
        self._act("browser_type", {**self._tab(), "ref": ref, "text": text, "mode": "insert_text", "replace": True})

    def scroll(self, direction: str) -> None:
        delta = {"down": SCROLL_PIXELS, "up": -SCROLL_PIXELS}[direction]
        scroll = {"action": "scroll", "delta_y": delta, "delivery_mode": "foreground"}
        self._act("browser_pointer", {**self._tab(), **scroll})

    def _act(self, tool: str, arguments: dict[str, object]) -> None:
        if _refused(self._call(tool, arguments)):
            raise CuaError("action-refused", fatal=False)

    def _call(self, tool: str, arguments: dict[str, object], *, images: bool = False) -> dict[str, object]:
        if tool not in TOOLS:
            raise CuaError("tool-not-admitted")
        result = self._request("tools/call", {"name": tool, "arguments": arguments})
        structured = result.get("structuredContent")
        if result.get("isError") is True or not isinstance(structured, dict):
            if tool in ACTIONS and isinstance(structured, dict) and _refused(structured):
                raise CuaError("action-refused", fatal=False)
            raise CuaError("tool-failed")
        if images:
            structured = {**structured, "_screenshot": _screenshot(result.get("content"))}
        return structured

    def _request(self, method: str, params: dict[str, object]) -> dict[str, object]:
        self._identifier += 1
        identifier = self._identifier
        message = {"jsonrpc": "2.0", "id": identifier, "method": method, "params": params}
        deadline = self._clock() + CALL_SECONDS
        if self.deadline is not None:
            deadline = min(deadline, self.deadline)
        try:
            self._channel.send(json.dumps(message, separators=(",", ":")).encode() + b"\n")
            return self._answer(identifier, deadline)
        except OSError as exc:
            raise CuaError("channel-lost") from exc

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


def _refused(structured: dict[str, object]) -> bool:
    """Whether the Driver's answer names an explicit refusal of the action, which had no effect."""
    return structured.get("effect") == "refused" or structured.get("status") == "refused"


def _refs(entries: object, budget: int) -> tuple[dict[str, frozenset[str]], list[str]]:
    """The refs the observation offers and their lines, within `budget` UTF-8 bytes: only a ref shown is offered.

    A ref outside the protocol's form is never offered, since no step could name it.
    """
    refs: dict[str, frozenset[str]] = {}
    lines: list[str] = []
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
    return refs, lines


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
