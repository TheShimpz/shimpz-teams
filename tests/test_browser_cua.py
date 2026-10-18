"""The Team Browser's MCP session with the Cua Driver: closed tools, exact binding, bounded observations."""

import base64
import json
import unittest

from browser import cua

WINDOW = {"pid": 20, "window_id": 7, "app_name": "Chromium-browser", "title": "about:blank - Chromium"}
PNG = cua.PNG_SIGNATURE + b"pixels"


def _bound(tabs=None, quality="exact"):
    tabs = [{"tab_id": "tab-1", "active": True}] if tabs is None else tabs
    return {"binding_quality": quality, "target_id": "bt-1", "tabs": tabs}


class FakeChannel:
    """Answers each request from scripted results, in order, and records every message Team sent."""

    def __init__(self, *results, raw=()):
        self.results = list(results)
        self.raw = list(raw)
        self.sent = []

    def send(self, data):
        self.sent.append(json.loads(data))

    def receive_line(self, deadline, limit):
        assert limit == cua.MAX_MESSAGE_BYTES and deadline > 0
        if self.raw:
            return self.raw.pop(0)
        request = self.sent[-1]
        result = self.results.pop(0)
        if isinstance(result, bytes):
            return result
        return json.dumps({"jsonrpc": "2.0", "id": request["id"], **result}).encode()

    def calls(self):
        return [
            (message["params"]["name"], message["params"]["arguments"])
            for message in self.sent
            if message.get("method") == "tools/call"
        ]


def _tool(structured, **extra):
    return {"result": {"structuredContent": structured, **extra}}


OPENING = (
    {"result": {"capabilities": {}}},
    _tool({"windows": [WINDOW]}),
    _tool({"status": "ok"}),
    _tool(_bound()),
)


def _opened(*results):
    channel = FakeChannel(*OPENING, *results)
    session = cua.CuaSession(channel, clock=lambda: 100.0)
    session.open()
    return session, channel


class CuaSessionTests(unittest.TestCase):
    def test_opening_binds_the_one_chromium_window_exactly(self):
        session, channel = _opened()
        self.assertEqual(channel.sent[0]["method"], "initialize")
        self.assertEqual(channel.sent[1], {"jsonrpc": "2.0", "method": "notifications/initialized"})
        window = {"pid": 20, "window_id": 7, "session": "task"}
        self.assertEqual(
            channel.calls(),
            [
                ("list_windows", {}),
                ("browser_prepare", {**window, "strategy": {"kind": "existing_profile"}}),
                ("get_browser_state", window),
            ],
        )
        self.assertEqual(session._tab(), {"session": "task", "target_id": "bt-1", "tab_id": "tab-1"})

    def test_opening_refuses_anything_but_one_exactly_bound_chromium_tab(self):
        cases = {
            "browser-window-unavailable": [
                (_tool({"windows": []}),),
                (_tool({"windows": [WINDOW, WINDOW]}),),
                (_tool({"windows": ["window"]}),),
                (_tool({"windows": None}),),
                (_tool({"windows": [{**WINDOW, "app_name": "Xterm"}]}),),
                (_tool({"windows": [{**WINDOW, "pid": "20"}]}),),
                (_tool({"windows": [{**WINDOW, "window_id": True}]}),),
            ],
            "browser-attach-refused": [(_tool({"windows": [WINDOW]}), _tool({"status": "refused"}))],
            "browser-binding-inexact": [
                (_tool({"windows": [WINDOW]}), _tool({"status": "ok"}), _tool(_bound(quality="heuristic"))),
                (_tool({"windows": [WINDOW]}), _tool({"status": "ok"}), _tool({**_bound(), "target_id": 3})),
                (_tool({"windows": [WINDOW]}), _tool({"status": "ok"}), _tool({**_bound(), "tabs": "tab"})),
                (_tool({"windows": [WINDOW]}), _tool({"status": "ok"}), _tool(_bound(tabs=[]))),
                (
                    _tool({"windows": [WINDOW]}),
                    _tool({"status": "ok"}),
                    _tool(_bound(tabs=[{"tab_id": "a", "active": True}, {"tab_id": "b", "active": True}])),
                ),
                (
                    _tool({"windows": [WINDOW]}),
                    _tool({"status": "ok"}),
                    _tool(_bound(tabs=["tab", {"tab_id": 3, "active": True}])),
                ),
            ],
        }
        for code, scripts in cases.items():
            for script in scripts:
                with self.subTest(code=code, script=script):
                    session = cua.CuaSession(FakeChannel({"result": {}}, *script))
                    with self.assertRaises(cua.CuaError) as raised:
                        session.open()
                    self.assertEqual((raised.exception.code, raised.exception.fatal), (code, True))

    def test_nothing_is_called_before_the_session_is_open(self):
        session = cua.CuaSession(FakeChannel())
        for action in (session._tab, session._bind, lambda: session.navigate("https://example.com")):
            with self.subTest(action=action), self.assertRaises(cua.CuaError) as raised:
                action()
            self.assertEqual(raised.exception.code, "browser-not-open")
        with self.assertRaises(cua.CuaError) as raised:
            session._call("run_script", {})
        self.assertEqual(raised.exception.code, "tool-not-admitted")

    def test_an_observation_is_bounded_text_with_the_refs_each_action_may_target(self):
        state = {
            "page": {"url": "https://example.com/\x07path", "title": "Example\nDomain"},
            "refs": [
                {"ref": "p1:0", "role": "textbox", "name": "Buscar\x00", "actions": ["type", "pointer"]},
                {"ref": "p1:1", "role": "button", "name": "B" * 500, "actions": ["click"]},
                {"ref": "p1:2", "role": "generic", "name": "x", "actions": ["dom_only"]},
                {"ref": 3, "actions": ["click"]},
                {"ref": "p1:4", "actions": "click"},
                "junk",
            ],
            "outline": '- heading "Editais"\n' + "x" * (cua.MAX_OBSERVATION_BYTES * 2),
        }
        session, channel = _opened(_tool(_bound()), _tool(state))
        observation = session.observe()
        self.assertEqual((observation.url, observation.title), ("https://example.com/ path", "Example Domain"))
        self.assertEqual(observation.refs, {"p1:0": frozenset({"type", "pointer"}), "p1:1": frozenset({"click"})})
        self.assertTrue(
            observation.text.startswith("url: https://example.com/ path\ntitle: Example Domain\nactions:\n")
        )
        self.assertIn('p1:0 textbox "Buscar" (pointer, type)', observation.text)
        self.assertIn(f'p1:1 button "{"B" * cua.MAX_NAME_CHARS}" (click)', observation.text)
        self.assertLessEqual(len(observation.text.encode()), cua.MAX_OBSERVATION_BYTES)
        self.assertIsNone(observation.screenshot)
        self.assertEqual(
            channel.calls()[-1],
            (
                "get_browser_state",
                {
                    "session": "task",
                    "target_id": "bt-1",
                    "tab_id": "tab-1",
                    "snapshot_format": "semantic_v2",
                    "include_screenshot": False,
                },
            ),
        )

    def test_an_observation_follows_the_active_tab_and_tolerates_a_sparse_snapshot(self):
        moved = _bound(tabs=[{"tab_id": "tab-1", "active": False}, {"tab_id": "tab-2", "active": True}])
        session, channel = _opened(_tool(moved), _tool({"page": "none", "refs": None, "outline": 3}))
        observation = session.observe()
        self.assertEqual((observation.url, observation.title, observation.refs), ("", "", {}))
        self.assertEqual(observation.text, "url: \ntitle: \nactions:\n\npage:\n")
        self.assertEqual(channel.calls()[-1][1]["tab_id"], "tab-2")

    def test_an_observation_keeps_at_most_its_bound_of_refs(self):
        refs = [
            {"ref": f"p1:{index}", "role": "link", "name": "n", "actions": ["click"]}
            for index in range(cua.MAX_REFS + 5)
        ]
        session, _channel = _opened(_tool(_bound()), _tool({"refs": refs}))
        self.assertEqual(len(session.observe().refs), cua.MAX_REFS)

    def test_a_requested_screenshot_is_one_bounded_png(self):
        def snapshot(content):
            return {"result": {"structuredContent": {"page": {}}, "content": content}}

        encoded = base64.b64encode(PNG).decode()
        cases = [
            ([{"type": "text", "text": "x"}, {"type": "image", "mimeType": "image/png", "data": encoded}], PNG),
            ([{"type": "image", "mimeType": "image/jpeg", "data": encoded}], None),
            ([{"type": "image", "mimeType": "image/png", "data": base64.b64encode(b"GIF89a").decode()}], None),
            ([{"type": "image", "mimeType": "image/png", "data": "not base64!"}], None),
            ([{"type": "image", "mimeType": "image/png", "data": 7}], None),
            ([{"type": "image", "mimeType": "image/png", "data": "A" * (cua.MAX_SCREENSHOT_BYTES * 2)}], None),
            (
                [
                    {
                        "type": "image",
                        "mimeType": "image/png",
                        "data": base64.b64encode(PNG + b"x" * cua.MAX_SCREENSHOT_BYTES).decode(),
                    }
                ],
                None,
            ),
            ("content", None),
        ]
        for content, expected in cases:
            with self.subTest(content=str(content)[:60]):
                session, channel = _opened(_tool(_bound()), snapshot(content))
                self.assertEqual(session.observe(screenshot=True).screenshot, expected)
                self.assertIs(channel.calls()[-1][1]["include_screenshot"], True)

    def test_actions_send_exactly_their_closed_arguments(self):
        session, channel = _opened(*(_tool({"status": "ok"}) for _ in range(5)))
        session.navigate("https://example.com/a")
        session.click("p1:1")
        session.type("p1:0", "inteligência artificial")
        session.scroll("down")
        session.scroll("up")
        tab = {"session": "task", "target_id": "bt-1", "tab_id": "tab-1"}
        self.assertEqual(
            channel.calls()[-5:],
            [
                ("browser_navigate", {**tab, "url": "https://example.com/a"}),
                ("browser_click", {**tab, "ref": "p1:1", "delivery_mode": "foreground"}),
                (
                    "browser_type",
                    {**tab, "ref": "p1:0", "text": "inteligência artificial", "mode": "insert_text", "replace": True},
                ),
                ("browser_pointer", {**tab, "action": "scroll", "delta_y": 600, "delivery_mode": "foreground"}),
                ("browser_pointer", {**tab, "action": "scroll", "delta_y": -600, "delivery_mode": "foreground"}),
            ],
        )

    def test_a_refused_page_action_is_not_fatal_but_a_failed_read_is(self):
        for result in (
            _tool({"effect": "refused"}),
            _tool({"status": "refused"}),
            _tool({"x": 1}, isError=True),
            {"result": {}},
        ):
            with self.subTest(result=result):
                session, _channel = _opened(result)
                with self.assertRaises(cua.CuaError) as raised:
                    session.click("p1:1")
                self.assertEqual((raised.exception.code, raised.exception.fatal), ("action-refused", False))
        session, _channel = _opened(_tool(_bound()), _tool({}, isError=True))
        with self.assertRaises(cua.CuaError) as raised:
            session.observe()
        self.assertEqual((raised.exception.code, raised.exception.fatal), ("tool-failed", True))

    def test_the_protocol_admits_only_the_answer_to_the_current_request(self):
        notification = json.dumps({"jsonrpc": "2.0", "method": "notifications/progress"}).encode()
        channel = FakeChannel({"result": {"capabilities": {}}}, raw=[notification])
        cua.CuaSession(channel)._request("initialize", {})
        for raw, code in (
            (b"\xff", "protocol-invalid"),
            (b"{not json", "protocol-invalid"),
            (b"[]", "protocol-invalid"),
            (json.dumps({"id": 99, "result": {}}).encode(), "protocol-invalid"),
            (json.dumps({"id": 1, "error": {"code": -1}}).encode(), "protocol-error"),
            (json.dumps({"id": 1, "result": []}).encode(), "protocol-error"),
        ):
            with self.subTest(raw=raw), self.assertRaises(cua.CuaError) as raised:
                cua.CuaSession(FakeChannel(raw=[raw]))._request("initialize", {})
            self.assertEqual((raised.exception.code, raised.exception.fatal), (code, True))


if __name__ == "__main__":
    unittest.main()
