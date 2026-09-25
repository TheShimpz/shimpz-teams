"""Deterministic Brain peer; import as perf.brain_peer to keep one class state."""

from __future__ import annotations

import json
import queue
import secrets
import sys
import time
from pathlib import Path
from typing import override

TEAM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(TEAM / "tests"))

import local_controller_docker_fixture as flow_fixture

OBJECTIVE = "Hello"
RESPONSE = {"intent": "ordinary-task", "query": "", "assistant_ids": [], "reply": ""}
CHAT_REPLY = "Measured reply."
SELECTED_ASSISTANT_ID = "shimpz-cloudflare"
CHAT_PROMPT = json.dumps({"files": [], "message": OBJECTIVE}, separators=(",", ":"), ensure_ascii=False)


class BrainPeer(flow_fixture.BrainLifecycleHandler):
    """Return only one closed intent decision and record the peer's own span."""

    delay_ms = 0
    selected_assistant = False
    dummy_key = secrets.token_urlsafe(24)
    spans: queue.Queue[float] = queue.Queue()
    turn_spans: queue.Queue[tuple[int, int]] = queue.Queue()

    @override
    def do_POST(self) -> None:
        if self.path not in {"/v1/intent-route", "/v1/turns"}:
            super().do_POST()
            return
        started = time.perf_counter_ns()
        length = self.headers.get("Content-Length", "")
        if not length.isdecimal() or int(length) > 16_384:
            self._reply(400, {"error": "invalid request"})
            return
        try:
            body = json.loads(self.rfile.read(int(length)))
        except UnicodeError, json.JSONDecodeError:
            self._reply(400, {"error": "invalid request"})
            return
        if not self._valid(body):
            self._reply(400, {"error": "invalid request"})
            return
        time.sleep(self.delay_ms / 1_000)
        result = (
            RESPONSE if self.path == "/v1/intent-route" else {"status": "completed", "reply": CHAT_REPLY, "actions": []}
        )
        self._reply(200, result)
        ended = time.perf_counter_ns()
        if self.path == "/v1/turns":
            self.turn_spans.put((started, ended))
        else:
            self.spans.put((ended - started) / 1_000_000)

    def _valid(self, body: object) -> bool:
        if not isinstance(body, dict) or not isinstance(body.get("provider"), dict):
            return False
        provider = body["provider"]
        if self.path == "/v1/turns":
            assistants = body["assistants"] if isinstance(body.get("assistants"), list) else None
            if self.selected_assistant:
                valid_assistants = (
                    assistants is not None
                    and len(assistants) == 1
                    and isinstance(assistants[0], dict)
                    and set(assistants[0]) == {"id", "genesis", "actions"}
                    and assistants[0]["id"] == SELECTED_ASSISTANT_ID
                    and isinstance(assistants[0]["genesis"], str)
                    and bool(assistants[0]["genesis"])
                    and isinstance(assistants[0]["actions"], list)
                    and bool(assistants[0]["actions"])
                )
            else:
                valid_assistants = assistants == []
            return (
                set(body) == {"thread_id", "team_name", "assistants", "provider", "message"}
                and isinstance(body["thread_id"], str)
                and bool(body["thread_id"])
                and body["team_name"] == "Demo Team"
                and valid_assistants
                and body["message"] == CHAT_PROMPT
                and provider == {"provider": "openai", "model": "gpt-6-sol", "api_key": self.dummy_key}
                and self.headers.get("Authorization", "").startswith("Bearer ")
            )
        return (
            set(body)
            == {
                "provider",
                "objective",
                "expected_intent",
                "candidates",
                "lifecycle_reference",
                "conversation",
                "language_exemplar",
            }
            and provider == {"provider": "openai", "model": "gpt-6-sol", "api_key": self.dummy_key}
            and body["objective"] == OBJECTIVE
            and body["expected_intent"] is None
            and body["candidates"] == []
            and body["lifecycle_reference"] is None
            and body["conversation"] == []
            and body["language_exemplar"] is None
            and self.headers.get("Authorization", "").startswith("Bearer ")
        )

    def _reply(self, status: int, body: dict[str, object]) -> None:
        encoded = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)
