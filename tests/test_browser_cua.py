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

    def send(self, data, deadline):
        assert deadline > 0
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


# The Driver's refusal once its grant on the window lapsed, suggesting a call on another window than Team's.
LAPSED = {
    "status": "refused",
    "refusal": {
        "code": "browser_consent_required",
        "detail": {
            "reason": "consumer_profile_endpoint_requires_grant",
            "next_action": "browser_prepare",
            "next_call": {"tool": "browser_prepare", "arguments": {"pid": 99, "window_id": 99, "session": "other"}},
        },
    },
}


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
    def test_counting_windows_binds_nothing_and_skips_the_drivers_own_overlay(self):
        overlay = {"pid": 21, "window_id": 9, "app_name": ""}
        channel = FakeChannel({"result": {"capabilities": {}}}, _tool({"windows": [WINDOW, WINDOW, overlay]}))
        session = cua.CuaSession(channel, clock=lambda: 100.0)
        session.initialize()
        self.assertEqual(session.chromium_windows(), 2)
        self.assertEqual(channel.calls(), [("list_windows", {})])
        for answer in ({"windows": "none"}, {"windows": [WINDOW, "window"]}):
            with self.subTest(answer=answer):
                channel = FakeChannel({"result": {"capabilities": {}}}, _tool(answer))
                session = cua.CuaSession(channel, clock=lambda: 100.0)
                session.initialize()
                with self.assertRaises(cua.CuaError):
                    session.chromium_windows()

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
                    # Each fails at its first answer: none is a tab still waiting for its active mark.
                    session = cua.CuaSession(FakeChannel({"result": {}}, *script), clock=lambda: 100.0)
                    with self.assertRaises(cua.CuaError) as raised:
                        session.open()
                    self.assertEqual((raised.exception.code, raised.exception.fatal), (code, True))

    def test_a_tab_mid_navigation_is_waited_for_until_one_is_active(self):
        now = [100.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        unmarked = _tool(_bound(tabs=[{"tab_id": "tab-2", "active": None}]))
        channel = FakeChannel(*OPENING, unmarked, unmarked, _tool(_bound(tabs=[{"tab_id": "tab-2", "active": True}])))
        session = cua.CuaSession(channel, clock=lambda: now[0], sleep=sleep)
        session.open()
        session._bind()
        self.assertEqual(session._tab()["tab_id"], "tab-2")
        self.assertEqual(slept, [cua.BIND_POLL_SECONDS] * 2)
        # One that never marks a tab active hung: it is lost once a call's whole bound is spent, or the task's.
        polls = int(cua.CALL_SECONDS / cua.BIND_POLL_SECONDS) + 1
        channel.results.extend([unmarked] * polls)
        with self.assertRaises(cua.CuaError) as raised:
            session._bind()
        self.assertEqual(raised.exception.code, "browser-binding-inexact")
        self.assertLessEqual(sum(slept[2:]), cua.CALL_SECONDS)
        self.assertGreater(sum(slept[2:]), cua.CALL_SECONDS - 1)
        session.deadline = now[0] + 1
        channel.results.extend([unmarked] * polls)
        before = len(slept)
        with self.assertRaises(cua.CuaError):
            session._bind()
        self.assertLessEqual(sum(slept[before:]), 1)

    def test_slow_answers_never_stretch_binding_past_its_one_bound(self):
        now = [100.0]
        deadlines: list[float] = []

        class Slow(FakeChannel):
            def receive_line(self, deadline, limit):
                deadlines.append(deadline)
                now[0] += 10
                return super().receive_line(deadline, limit)

        unmarked = _tool(_bound(tabs=[{"tab_id": "tab-2", "active": None}]))
        channel = Slow(*OPENING, *[unmarked] * 10)
        session = cua.CuaSession(channel, clock=lambda: now[0], sleep=lambda seconds: None)
        session.open()
        started = now[0]
        deadlines.clear()
        with self.assertRaises(cua.CuaError):
            session._bind()
        self.assertTrue(deadlines)
        self.assertTrue(all(deadline <= started + cua.CALL_SECONDS for deadline in deadlines))
        self.assertLessEqual(now[0], started + cua.CALL_SECONDS + 10)

    def test_a_malformed_active_tab_never_hides_a_second_one(self):
        tabs = [{"tab_id": "a", "active": True}, {"tab_id": 3, "active": True}]
        session, _channel = _opened(_tool(_bound(tabs=tabs)))
        with self.assertRaises(cua.CuaError) as raised:
            session._bind()
        self.assertEqual(raised.exception.code, "browser-binding-inexact")

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
                {"ref": "button\n", "actions": ["click"]},
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

    def test_an_observation_offers_only_the_refs_it_shows_within_its_byte_bound(self):
        refs = [
            {"ref": f"p1:{index}", "role": "link", "name": "ç" * cua.MAX_NAME_CHARS, "actions": ["click"]}
            for index in range(cua.MAX_REFS)
        ]
        session, _channel = _opened(_tool(_bound()), _tool({"page": {"url": "é" * 2048}, "refs": refs}))
        observation = session.observe()
        self.assertLessEqual(len(observation.text.encode()), cua.MAX_OBSERVATION_BYTES)
        self.assertLess(len(observation.refs), cua.MAX_REFS)
        self.assertEqual(
            [line.split()[0] for line in observation.text.split("\nactions:\n")[1].split("\npage:\n")[0].splitlines()],
            list(observation.refs),
        )

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
                (
                    "browser_pointer",
                    {**tab, "action": "scroll", "delta_y": 600, "x": 640, "y": 400, "delivery_mode": "foreground"},
                ),
                (
                    "browser_pointer",
                    {**tab, "action": "scroll", "delta_y": -600, "x": 640, "y": 400, "delivery_mode": "foreground"},
                ),
            ],
        )

    def test_a_navigation_that_failed_for_a_network_error_is_its_own_outcome_not_a_loss(self):
        def failed(text):
            return {
                "result": {
                    "content": [{"type": "text", "text": text}],
                    "isError": True,
                    "structuredContent": {"code": "tool_invocation_failed"},
                }
            }

        session, _channel = _opened(failed("navigation failed: net::ERR_TUNNEL_CONNECTION_FAILED"))
        with self.assertRaises(cua.CuaError) as raised:
            session.navigate("https://www.python.org")
        self.assertEqual((raised.exception.code, raised.exception.fatal), ("navigation-failed", False))
        # Only the Driver's exact error envelope: an answer not marked an error, an image, or an extra item is a loss.
        text = "navigation failed: net::ERR_TUNNEL_CONNECTION_FAILED"
        for result in (
            {"result": {"content": [{"type": "text", "text": text}], "isError": False}},
            {"result": {"content": [{"type": "text", "text": text}]}},
            {"result": {"content": [{"type": "image", "text": text}], "isError": True}},
            {"result": {"content": [{"type": "text", "text": text}, "x"], "isError": True}},
            {"result": {"content": [{"type": "text", "text": text}, {"type": "text", "text": text}], "isError": True}},
        ):
            with self.subTest(result=result):
                session, _channel = _opened(result)
                with self.assertRaises(cua.CuaError) as raised:
                    session.navigate("https://example.com")
                self.assertEqual((raised.exception.code, raised.exception.fatal), ("tool-failed", True))
        # Any other failure text, or the same text on another action, may mean the Browser is lost.
        for action, result in (
            (lambda s: s.navigate("https://example.com"), failed("navigation failed: something else")),
            (lambda s: s.navigate("https://example.com"), failed("navigation failed: net::ERR_X\nmore")),
            (lambda s: s.click("p1:1"), failed("navigation failed: net::ERR_TUNNEL_CONNECTION_FAILED")),
        ):
            with self.subTest(result=result):
                session, _channel = _opened(result)
                with self.assertRaises(cua.CuaError) as raised:
                    action(session)
                self.assertEqual((raised.exception.code, raised.exception.fatal), ("tool-failed", True))

    def test_only_an_explicitly_refused_page_action_is_not_fatal(self):
        for result in (
            _tool({"effect": "refused"}),
            _tool({"status": "refused"}),
            _tool({"effect": "refused"}, isError=True),
        ):
            with self.subTest(result=result):
                session, _channel = _opened(result)
                with self.assertRaises(cua.CuaError) as raised:
                    session.click("p1:1")
                self.assertEqual((raised.exception.code, raised.exception.fatal), ("action-refused", False))
        # An error that names no refusal may have had an effect: the task must stop.
        for result in (_tool({"x": 1}, isError=True), {"result": {}}, {"result": {"isError": True}}):
            with self.subTest(result=result):
                session, _channel = _opened(result)
                with self.assertRaises(cua.CuaError) as raised:
                    session.click("p1:1")
                self.assertEqual((raised.exception.code, raised.exception.fatal), ("tool-failed", True))
        session, _channel = _opened(_tool(_bound()), _tool({}, isError=True))
        with self.assertRaises(cua.CuaError) as raised:
            session.observe()
        self.assertEqual((raised.exception.code, raised.exception.fatal), ("tool-failed", True))

    def test_a_lapsed_grant_is_renewed_on_the_admitted_window_and_binding_goes_on(self):
        window = {"pid": 20, "window_id": 7, "session": "task"}
        prepare = ("browser_prepare", {**window, "strategy": {"kind": "existing_profile"}})
        for envelope in ({}, {"isError": True}):
            with self.subTest(envelope=envelope):
                # The refusal's own suggested call names another window: Team renews only the window it admitted.
                session, channel = _opened(_tool(LAPSED, **envelope), _tool({"status": "ok"}), _tool(_bound()))
                del channel.sent[:]
                session._bind()
                self.assertEqual(
                    channel.calls(), [("get_browser_state", window), prepare, ("get_browser_state", window)]
                )
        # A grant that lapses again at once in the same binding is not renewed twice.
        session, _channel = _opened(_tool(LAPSED), _tool({"status": "ok"}), _tool(LAPSED))
        with self.assertRaises(cua.CuaError) as raised:
            session._bind()
        self.assertEqual((raised.exception.code, raised.exception.fatal), ("browser-binding-inexact", True))
        # A renewal the Driver refuses is the attach refusal it always was.
        session, _channel = _opened(_tool(LAPSED), _tool({"status": "refused"}))
        with self.assertRaises(cua.CuaError) as raised:
            session._bind()
        self.assertEqual(raised.exception.code, "browser-attach-refused")
        # Another consent refusal is no lapsed grant: it is never answered with a renewal, only read again.
        other = {**LAPSED, "refusal": {**LAPSED["refusal"], "detail": {"reason": "user_denied"}}}
        session, channel = _opened(_tool(other), _tool(_bound()))
        session._bind()
        self.assertEqual([name for name, _ in channel.calls()[3:]], ["get_browser_state"] * 2)

    def test_an_old_grant_is_renewed_before_binding_within_the_same_bound(self):
        now = [100.0]
        channel = FakeChannel(*OPENING, _tool({"status": "ok"}), _tool(_bound()), _tool(_bound()))
        session = cua.CuaSession(channel, clock=lambda: now[0])
        session.open()
        now[0] += cua.REGRANT_SECONDS
        session._bind()
        self.assertEqual([name for name, _ in channel.calls()[3:]], ["browser_prepare", "get_browser_state"])
        # A fresh grant binds without renewing.
        session._bind()
        self.assertEqual([name for name, _ in channel.calls()[5:]], ["get_browser_state"])
        # A deadline already spent never sends the renewal.
        session.deadline = now[0]
        now[0] += cua.REGRANT_SECONDS
        sent = len(channel.sent)
        with self.assertRaises(cua.CuaError) as raised:
            session._bind()
        self.assertEqual((raised.exception.code, len(channel.sent)), ("deadline-passed", sent))

    def test_a_grant_lapsed_at_capture_is_renewed_and_the_page_captured_once_more(self):
        page = _tool({"page": {"url": "https://example.com/", "title": "Example"}, "refs": [], "outline": "Hi"})
        session, channel = _opened(_tool(_bound()), _tool(LAPSED), _tool({"status": "ok"}), _tool(_bound()), page)
        observation = session.observe()
        self.assertEqual(observation.url, "https://example.com/")
        names = [name for name, _ in channel.calls()[3:]]
        self.assertEqual(names, ["get_browser_state"] * 2 + ["browser_prepare"] + ["get_browser_state"] * 2)
        session, _channel = _opened(
            _tool(_bound()), _tool(LAPSED), _tool({"status": "ok"}), _tool(_bound()), _tool(LAPSED)
        )
        with self.assertRaises(cua.CuaError) as raised:
            session.observe()
        self.assertEqual(raised.exception.code, "browser-binding-inexact")

    def test_a_refused_page_read_is_asked_again_until_its_one_bound_is_spent(self):
        now = [100.0]
        slept = []

        def sleep(seconds):
            slept.append(seconds)
            now[0] += seconds

        refused = {"status": "refused", "refusal": {"code": "browser_route_unavailable", "message": "frame gone"}}
        page = _tool({"page": {"url": "https://example.com/", "title": "Example"}, "refs": [], "outline": "Hi"})
        for envelope in ({}, {"isError": True}):
            with self.subTest(envelope=envelope):
                channel = FakeChannel(*OPENING, _tool(_bound()), _tool(refused, **envelope), _tool(_bound()), page)
                session = cua.CuaSession(channel, clock=lambda: now[0], sleep=sleep)
                session.open()
                self.assertEqual(session.observe().url, "https://example.com/")
        self.assertEqual(slept, [cua.READ_RETRY_SECONDS] * 2)
        # A read still refused once no whole call fits in the read's bound is a loss.
        reads = int((cua.READ_SECONDS - cua.CALL_SECONDS) / cua.READ_RETRY_SECONDS) + 1
        channel = FakeChannel(*OPENING, _tool(_bound()), *[_tool(refused), _tool(_bound())] * reads)
        session = cua.CuaSession(channel, clock=lambda: now[0], sleep=sleep)
        session.open()
        with self.assertRaises(cua.CuaError) as raised:
            session.observe()
        self.assertEqual((raised.exception.code, raised.exception.fatal), ("browser-read-refused", True))
        self.assertEqual(len(channel.results), 1)
        # A slow refusal leaves the next attempt a whole call within the read's bound, never a part of one.
        slow = FakeChannel(*OPENING, _tool(_bound()), _tool(refused), _tool(_bound()), page)
        session = cua.CuaSession(slow, clock=lambda: now[0], sleep=sleep)
        session.open()
        sent = slow.send

        def late(data, deadline):
            if b"get_browser_state" in data and b"semantic_v2" in data:
                self.assertGreaterEqual(deadline - now[0], cua.CALL_SECONDS)
                now[0] += 20.0
            sent(data, deadline)

        slow.send = late
        self.assertEqual(session.observe().url, "https://example.com/")
        # A slow binding that leaves no whole call in the read's bound ends the read without asking it again.
        rebinding = FakeChannel(*OPENING, _tool(_bound()), _tool(refused), _tool(_bound()), page)
        session = cua.CuaSession(rebinding, clock=lambda: now[0], sleep=sleep)
        session.open()
        sent_first = rebinding.send

        def slow_bind(data, deadline):
            message = json.loads(data)
            arguments = message.get("params", {}).get("arguments", {})
            # The refused read and the binding after it each take the Driver's whole 20-second wait.
            rebind = message.get("method") == "tools/call" and "pid" in arguments and len(rebinding.sent) > 4
            if rebind or "snapshot_format" in arguments:
                now[0] += 20.0
            sent_first(data, deadline)

        rebinding.send = slow_bind
        with self.assertRaises(cua.CuaError) as raised:
            session.observe()
        self.assertEqual(raised.exception.code, "browser-read-refused")
        self.assertEqual(rebinding.results, [page])

    def test_a_refused_bind_is_asked_again_only_while_the_slowest_refusal_fits(self):
        now = [100.0]
        refused = {"status": "refused", "refusal": {"code": "authorization_host_failed", "message": "timed out"}}
        # A refusal that came at once is asked again and the binding goes on.
        channel = FakeChannel(*OPENING, _tool(refused, isError=True), _tool(_bound()))
        session = cua.CuaSession(channel, clock=lambda: now[0], sleep=lambda seconds: None)
        session.open()
        session._bind()
        # One that took the Driver's whole wait leaves no room for another: Chromium refuses the read.
        channel = FakeChannel(*OPENING, _tool(refused, isError=True))
        session = cua.CuaSession(channel, clock=lambda: now[0], sleep=lambda seconds: None)
        session.open()
        sent = channel.send

        def slow(data, deadline):
            now[0] += cua.REFUSAL_SECONDS - 1
            sent(data, deadline)

        channel.send = slow
        with self.assertRaises(cua.CuaError) as raised:
            session._bind()
        self.assertEqual((raised.exception.code, len(channel.results)), ("browser-read-refused", 0))

    def test_an_action_refused_for_a_lapsed_grant_renews_it_and_is_never_repeated(self):
        ended = {"status": "refused", "refusal": {"code": cua.ENDED_CODE, "message": cua.ENDED_MESSAGE}}
        for lapsed in (LAPSED, ended):
            with self.subTest(lapsed=lapsed):
                session, channel = _opened(_tool(lapsed, isError=True), _tool({"status": "ok"}))
                with self.assertRaises(cua.CuaError) as raised:
                    session.click("p1:1")
                self.assertEqual((raised.exception.code, raised.exception.fatal), ("action-refused", False))
                self.assertEqual([name for name, _ in channel.calls()[3:]], ["browser_click", "browser_prepare"])
        # The same code with another message is an ordinary refusal: nothing is renewed.
        other = {"status": "refused", "refusal": {"code": cua.ENDED_CODE, "message": "another reason"}}
        session, channel = _opened(_tool(other, isError=True))
        with self.assertRaises(cua.CuaError) as raised:
            session.click("p1:1")
        self.assertEqual([name for name, _ in channel.calls()[3:]], ["browser_click"])

    def test_a_failed_channel_is_a_fatal_loss_and_no_call_outlives_the_task(self):
        class Broken(FakeChannel):
            def receive_line(self, deadline, limit):
                self.deadlines.append(deadline)
                raise OSError("closed")

        channel = Broken()
        channel.deadlines = []
        session = cua.CuaSession(channel, clock=lambda: 100.0)
        session.deadline = 110.0
        with self.assertRaises(cua.CuaError) as raised:
            session._request("initialize", {})
        self.assertEqual(
            (raised.exception.code, raised.exception.fatal, channel.deadlines), ("channel-lost", True, [110.0])
        )
        session.deadline = None
        with self.assertRaises(cua.CuaError):
            session._request("initialize", {})
        self.assertEqual(channel.deadlines[-1], 100.0 + cua.CALL_SECONDS)
        # Once the task's deadline passed, nothing more is sent.
        session.deadline = 100.0
        sent = len(channel.sent)
        with self.assertRaises(cua.CuaError) as raised:
            session._request("initialize", {})
        self.assertEqual((raised.exception.code, len(channel.sent)), ("deadline-passed", sent))

    def test_a_channel_that_refuses_the_initialized_notification_is_lost(self):
        class Refusing(FakeChannel):
            def send(self, data, deadline):
                if b"notifications/initialized" in data:
                    raise OSError("closed")
                super().send(data, deadline)

        session = cua.CuaSession(Refusing({"result": {"capabilities": {}}}), clock=lambda: 100.0)
        with self.assertRaises(cua.CuaError) as raised:
            session.open()
        self.assertEqual(raised.exception.code, "channel-lost")

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
