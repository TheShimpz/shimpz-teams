"""A recording turn's memory-only trace: kept values, the turn protection set, and their bounds (ADR-0101)."""

from __future__ import annotations

import unittest

from routine import trace

SECRET_SCHEMA = {
    "type": "object",
    "properties": {
        "zones": {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}},
        "account": {"type": "object", "properties": {"id": {"type": "string"}, "token": {"type": "string"}}},
        "hidden": {"type": "string", "writeOnly": True},
    },
}
CREDENTIAL = "sk-" + "A1b2C3d4" * 6


def _occurrence(**changes: object) -> trace.Occurrence:
    values = {
        "operation_id": "6f1c2b8e-3a4d-4c5e-9f60-718293a4b5c6",
        "assistant": "cloudflare",
        "action": "list-zones",
        "pin": "sha256:" + "a" * 64,
        "read_only": True,
        "dispatched_at": 1_760_000_000,
        "input": trace.keep({}, {}, ()),
        "result": trace.keep({"zones": []}, SECRET_SCHEMA, ()),
    }
    values.update(changes)
    return trace.Occurrence(**values)


class KeepTests(unittest.TestCase):
    def test_secret_positions_credentials_and_protected_values_are_withheld_whole(self) -> None:
        value = {
            "zones": [{"id": "z1"}, {"id": "holds injected-value here"}],
            "account": {"id": "acc", "token": "t"},
            "hidden": "h",
            "note": CREDENTIAL,
            "map": {CREDENTIAL: 1},
            "plain": 5,
        }
        kept = trace.keep(value, SECRET_SCHEMA, ("injected-value", ""))
        self.assertEqual(kept.withheld, frozenset({"/zones/1/id", "/account/token", "/hidden", "/note", "/map"}))
        self.assertEqual(kept.value["zones"][0], {"id": "z1"})
        self.assertIsNone(kept.value["note"])
        self.assertEqual(kept.value["plain"], 5)
        self.assertFalse(kept.oversize)

    def test_available_sees_withheld_ancestors_descendants_and_the_root(self) -> None:
        kept = trace.Kept({"a": {"b": None}}, frozenset({"/a/b"}))
        self.assertTrue(kept.available("/a/c"))
        self.assertTrue(kept.available("/ab"))
        self.assertFalse(kept.available("/a/b"))
        self.assertFalse(kept.available("/a/b/c"))
        self.assertFalse(kept.available("/a"))
        self.assertFalse(kept.available(""))
        self.assertFalse(trace.Kept(None, frozenset({""})).available("/x"))

    def test_an_oversize_value_is_withheld_whole(self) -> None:
        kept = trace.keep("x" * (trace.MAX_KEPT_BYTES + 1), {}, ())
        self.assertEqual((kept.value, kept.withheld, kept.oversize), (None, frozenset({""}), True))

    def test_an_unwalkable_schema_withholds_its_position(self) -> None:
        deep: dict[str, object] = {}
        node = deep
        for _ in range(200):
            node["allOf"] = [{}]
            node = node["allOf"][0]
        kept = trace.keep({"a": 1}, deep, ())
        self.assertEqual(kept.withheld, frozenset({""}))

    def test_escaped_keys_name_their_pointer(self) -> None:
        kept = trace.keep({"a/b": {"c~d": CREDENTIAL}}, {}, ())
        self.assertEqual(kept.withheld, frozenset({"/a~1b/c~0d"}))


class SecretValuesTests(unittest.TestCase):
    def test_strings_at_secret_positions_are_collected(self) -> None:
        value = {"account": {"token": "tok-1", "id": "acc"}, "hidden": "h-1", "zones": [{"id": "z"}]}
        self.assertEqual(sorted(trace.secret_values(value, SECRET_SCHEMA)), ["h-1", "tok-1"])
        nested = {"hidden": {"inner": ["deep-1", 3]}}
        self.assertEqual(trace.secret_values(nested, SECRET_SCHEMA), ("deep-1",))
        self.assertEqual(trace.secret_values({"a": "b"}, None), ())

    def test_an_unwalkable_schema_yields_every_string(self) -> None:
        deep: dict[str, object] = {}
        node = deep
        for _ in range(200):
            node["allOf"] = [{}]
            node = node["allOf"][0]
        self.assertEqual(trace.secret_values({"a": "b"}, deep), ("b",))


class ExposesTests(unittest.TestCase):
    def test_any_string_or_key_holding_a_protected_value_exposes_it(self) -> None:
        protected = frozenset({"tok-1"})
        self.assertTrue(trace.exposes("x tok-1 y", protected))
        self.assertTrue(trace.exposes({"a": ["tok-1"]}, protected))
        self.assertTrue(trace.exposes({"tok-1": 1}, protected))
        self.assertFalse(trace.exposes({"a": [1, None, True, "tok"]}, protected))
        self.assertFalse(trace.exposes("tok-1", frozenset()))


class ProtectionTests(unittest.TestCase):
    def test_protection_grows_and_saturation_is_irreversible(self) -> None:
        protection = trace.Protection().grow(("a", "", "b", "a"))
        self.assertEqual((protection.values, protection.lost), (frozenset({"a", "b"}), False))
        full = trace.Protection().grow(tuple(f"v{index}" for index in range(trace.MAX_PROTECTED_VALUES)))
        self.assertFalse(full.lost)
        lost = full.grow(("one-more",))
        self.assertTrue(lost.lost)
        self.assertTrue(lost.grow(()).lost)
        self.assertTrue(trace.Protection().grow(("x" * (trace.MAX_PROTECTED_BYTES + 1),)).lost)
        self.assertEqual(full.grow(("v1",)), full)


class TraceTests(unittest.TestCase):
    def test_occurrences_append_in_dispatch_order(self) -> None:
        recorded = trace.Trace("2026-10-05", 1_760_000_000)
        recorded = recorded.add(_occurrence()).add(_occurrence(action="list-dns-records"))
        self.assertEqual([item.action for item in recorded.occurrences], ["list-zones", "list-dns-records"])
        self.assertGreater(recorded.size, 0)

    def test_the_occurrence_and_byte_bounds_refuse_a_larger_recording(self) -> None:
        recorded = trace.Trace(None, 1)
        occurrence = _occurrence()
        for _ in range(trace.MAX_OCCURRENCES):
            recorded = recorded.add(occurrence)
        with self.assertRaises(trace.TraceError) as raised:
            recorded.add(occurrence)
        self.assertEqual(raised.exception.code, "routine-recording-too-large")
        large = _occurrence(result=trace.keep("x" * (trace.MAX_KEPT_BYTES - 16), {}, ()))
        recorded = trace.Trace(None, 1)
        with self.assertRaises(trace.TraceError):
            for _ in range(5):
                recorded = recorded.add(large)


if __name__ == "__main__":
    unittest.main()
