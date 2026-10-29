"""Team's HTTP protocol reads origins and URLs as plain ASCII authorities and refuses non-string closed members."""

import json
import unittest
from pathlib import Path

from protocol.http.v1 import browser, sign_in, websocket

VECTORS = json.loads((Path(__file__).resolve().parents[1] / "protocol/http/v1/vectors.json").read_bytes())


class UrlCanonicalizationTests(unittest.TestCase):
    def test_every_url_vector_holds(self) -> None:
        group = VECTORS["url_canonicalization"]
        for case in [*group["named"], *group["corpus"]]:
            with self.subTest(url=case["url"]):
                self.assertEqual(websocket.canonical_origin(case["url"]), case["origin"])
                self.assertEqual(browser.canonical_url(case["url"]), case["browser_url"])
                self.assertEqual(sign_in.start_site(case["url"]), case["start_site"])

    def test_web_authority_is_an_ascii_authority_without_brackets(self) -> None:
        self.assertEqual(
            browser.web_authority("HTTPS://user@Example.com:8443/a?b#c"), ("https", "user@Example.com:8443")
        )
        self.assertEqual(browser.web_authority("http://"), ("http", ""))
        for url in ("ftp://example.com", "https:/example.com", "http\u017f://x", "https://\u212aeep.com/"):
            with self.subTest(url=url):
                self.assertIsNone(browser.web_authority(url))
        for url in ("https://[::1]/", "https://a]/", "https://a[/"):
            with self.subTest(url=url):
                self.assertIsNone(browser.web_authority(url))
        self.assertIsNone(browser.canonical_url("https://\u212aeep.com/"))
        self.assertIsNone(sign_in.start_site("https://\u212aeep.com/"))
        self.assertEqual(sign_in.start_site("HTTPS://Example.com:0443/"), "example.com")

    def test_origins_are_printable_ascii_without_brackets_queries_or_fragments(self) -> None:
        self.assertEqual(websocket.canonical_origin("HTTP://Admin.Example:8443"), "http://admin.example:8443")
        for value in (
            " http://a",
            "http://a\x7f",
            "http://\u00e9",
            "http://[::1]",
            "http://a?",
            "http://a#",
            "http://a]",
        ):
            with self.subTest(value=value):
                self.assertIsNone(websocket.canonical_origin(value))


class ClosedStringMemberTests(unittest.TestCase):
    def test_a_closed_member_that_is_no_string_is_refused_not_hashed(self) -> None:
        current = VECTORS["sign_in_current"]["valid"][1]["sign_in"]
        self.assertIsNone(sign_in.canonical_sign_in({**current, "state": ["ready"]}))
        delete = VECTORS["sign_in_delete"]["valid"][0]
        self.assertIsNone(sign_in.canonical_delete({**delete, "factor": {"totp": 1}}))
        self.assertIsNone(sign_in.canonical_deleted({"deletion": ["deleted"]}))
        self.assertIsNone(sign_in.canonical_deleted({"deletion": "forgotten"}))
        self.assertIsNone(sign_in.canonical_deleted([]))
        self.assertEqual(sign_in.canonical_deleted({"deletion": "pending"}), {"deletion": "pending"})
        terminal = VECTORS["browser"]["terminal"]["valid"][0]
        self.assertIsNone(browser.canonical_terminal({**terminal, "lost": [{"name": "a", "reason": ["empty"]}]}))
        self.assertIsNotNone(browser.canonical_terminal({**terminal, "lost": [{"name": "a", "reason": "empty"}]}))
        self.assertIsNone(browser.canonical_file_reference({**terminal["files"][0], "name": None}))


if __name__ == "__main__":
    unittest.main()
