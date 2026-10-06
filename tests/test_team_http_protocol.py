"""Integrity gate for the producer-owned Team HTTP protocol."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import re
import runpy
import sys
import unittest
from pathlib import Path
from unittest import mock

from protocol.http.v1 import identifiers, payload, routine

PROTOCOL = Path(__file__).resolve().parents[1] / "protocol" / "http" / "v1"
ASSISTANT_PROTOCOL = Path(__file__).resolve().parents[1] / "protocol" / "assistant" / "v1"
MANIFEST = PROTOCOL / "contract-files.sha256"
ROW = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9._-]+)")


class TeamHttpProtocolTests(unittest.TestCase):
    def test_manifest_covers_and_digests_every_produced_artifact(self) -> None:
        matches = [ROW.fullmatch(line) for line in MANIFEST.read_text(encoding="ascii").splitlines()]
        self.assertTrue(matches)
        self.assertTrue(all(matches))
        expected = {match[2]: match[1] for match in matches if match is not None}
        actual = {path.name for path in PROTOCOL.iterdir() if path.is_file() and path != MANIFEST}
        self.assertEqual(set(expected), actual)
        for filename, digest in expected.items():
            self.assertEqual(hashlib.sha256((PROTOCOL / filename).read_bytes()).hexdigest(), digest)


class FlatVerifierTests(unittest.TestCase):
    """`verify.py` runs every module of the protocol directory flat, as each consumer's copy does."""

    def test_the_verifier_imports_the_protocol_modules_flat_from_their_directory(self) -> None:
        names = (
            "identifiers",
            "payload",
            "progress",
            "purpose",
            "routine",
            "strict_json",
            "supervisor",
            "turn",
            "websocket",
        )
        saved = {name: sys.modules.pop(name) for name in names if name in sys.modules}
        output = io.StringIO()
        try:
            with mock.patch.object(sys, "path", [str(PROTOCOL), *sys.path]), contextlib.redirect_stdout(output):
                runpy.run_path(str(PROTOCOL / "verify.py"), run_name="team_protocol_verifier")
        finally:
            for name in names:
                sys.modules.pop(name, None)
            sys.modules.update(saved)
        self.assertIn("golden vectors are valid", output.getvalue())


class IdentifierAuthorityTests(unittest.TestCase):
    """Assistant ids and Assistant-declared identifiers follow the pinned Developers published-Assistant protocol."""

    def test_developers_identifier_grammars_and_bounds_are_the_protocol_definitions(self) -> None:
        manifest = json.loads((ASSISTANT_PROTOCOL / "manifest.schema.json").read_bytes())["$defs"]
        machine = json.loads((ASSISTANT_PROTOCOL / "machine-contract.schema.json").read_bytes())["$defs"]
        self.assertEqual(manifest["assistantIdentifier"]["pattern"], identifiers.ASSISTANT_ID_PATTERN)
        self.assertEqual(manifest["assistantIdentifier"]["maxLength"], identifiers.MAX_ASSISTANT_ID_CHARS)
        for definitions in (manifest, machine):
            self.assertEqual(definitions["identifier"]["pattern"], identifiers.IDENTIFIER_PATTERN)
            self.assertEqual(definitions["identifier"]["maxLength"], identifiers.MAX_IDENTIFIER_CHARS)
        self.assertIs(payload.canonical_assistant_id, identifiers.canonical_assistant_id)
        self.assertIs(payload.canonical_identifier, identifiers.canonical_identifier)


class LocalizedChallengeContractTests(unittest.TestCase):
    """The ADR-0091 localization fields of a human challenge and a Routine challenge opening."""

    def setUp(self) -> None:
        self.vectors = json.loads((PROTOCOL / "vectors.json").read_bytes())

    def test_rendered_copy_admits_exactly_the_request_copy_fields(self) -> None:
        for case in self.vectors["rendered_copy"]["valid"]:
            self.assertEqual(payload.canonical_rendered(case["rendered"], case["request"]), case["rendered"])
        for case in self.vectors["rendered_copy"]["invalid"]:
            self.assertIsNone(payload.canonical_rendered(case["rendered"], case["request"]))

    def test_pack_digest_challenge_open_and_snapshot_summary_locale_are_closed(self) -> None:
        for name, admit in (
            ("pack_digest", payload.canonical_pack_digest),
            ("routine_challenge_open", routine.canonical_challenge_open),
            ("snapshot_summary", payload.canonical_snapshot_summary),
        ):
            for value in self.vectors[name]["valid"]:
                self.assertEqual(admit(value), value)
            for value in self.vectors[name]["invalid"]:
                self.assertIsNone(admit(value))


class RoutineDiagnosticsContractTests(unittest.TestCase):
    """The ADR-0092 execution details a Supervisor reads for one Routine run."""

    def test_team_admits_exactly_the_published_diagnostics_vectors(self) -> None:
        vectors = json.loads((PROTOCOL / "vectors.json").read_bytes())["routine_diagnostics"]
        for value in vectors["valid"]:
            self.assertEqual(routine.canonical_diagnostics(value), value)
        for value in vectors["invalid"]:
            self.assertIsNone(routine.canonical_diagnostics(value))

    def test_a_diagnostic_is_exactly_one_failure_or_one_safe_condition(self) -> None:
        self.assertIsNone(routine.canonical_failure([]))
        self.assertIsNone(routine.canonical_diagnostic([]))
        self.assertFalse(routine._diagnostic_text("lone \ud800 surrogate"))
        self.assertFalse(routine._diagnostic_text(7))


WIDE = "\U0001d538"  # One printable character that encodes to four UTF-8 bytes.
# The producer bounds a Local Team holds its identifiers to: an installed Assistant's id and a reviewed Action's id.
ASSISTANT = "a" * identifiers.MAX_ASSISTANT_ID_CHARS
ACTION = "b" * identifiers.MAX_ACTION_ID_CHARS


def _filler(index: int, size: int) -> dict[str, object]:
    """One ASCII literal input that adds exactly ``size`` (49 to 292) bytes to a projection, its comma included."""
    member = min(routine.MAX_MEMBER_CHARS, size - 45)
    return {"member": f"z{index:03d}" + "m" * (member - 4), "source": "literal", "value": "v" * (size - 44 - member)}


def _bounded_step(position: int = 1, size: int = routine.MAX_STEP_VIEW_BYTES) -> dict[str, object]:
    """One projected step of exactly ``size`` encoded bytes, its inputs at their character bounds in four-byte text."""
    stored = sorted(f"s{index:02d}" + "s" * 37 for index in range(routine.MAX_STEP_STORED_INPUTS))
    step = {
        "position": position,
        "assistant": ASSISTANT,
        "action": ACTION,
        "read_only": True,
        "inputs": [],
        "stored_inputs": stored,
    }
    for index in range(routine.MAX_STEP_INPUTS):
        member = f"m{index:03d}" + WIDE * (routine.MAX_MEMBER_CHARS - 4)
        item = {"member": member, "source": "literal", "value": WIDE * routine.MAX_PREVIEW_CHARS}
        if routine.encoded_bytes(step) + routine.encoded_bytes(item) + 1 > size - 49:
            break
        step["inputs"].append(item)
    room = size - routine.encoded_bytes(step)
    count = -(-room // 292)
    sizes = [room // count + (1 if index < room % count else 0) for index in range(count)]
    step["inputs"].extend(_filler(index, item) for index, item in enumerate(sizes))
    return step


def _largest_summary() -> dict[str, object]:
    """A plan summary at every bound: sixteen runs of the longest identifiers, the largest counts and revision."""
    runs = [[ASSISTANT[:-2] + f"{index:02d}", ACTION, 1 if index < 15 else 241] for index in range(16)]
    return {
        "revision": 2**31 - 1,
        "plan_digest": "sha256:" + "f" * 64,
        "steps": routine.MAX_ROUTINE_STEPS,
        "actions": runs,
        "more": 0,
    }


class RoutineListBoundTests(unittest.TestCase):
    """A Team's whole Routine list fits its allowance with every field at its producer bound (ADR-0086)."""

    def test_a_step_projection_is_refused_one_byte_past_its_bound_or_with_a_lone_surrogate(self) -> None:
        step = _bounded_step(7)
        self.assertEqual(routine.encoded_bytes(step), routine.MAX_STEP_VIEW_BYTES)
        self.assertEqual(routine.canonical_step(step, 7), step)
        # One ASCII character becomes a two-byte one: the same characters, one byte more.
        filler = step["inputs"][-1]
        filler["value"] = "\u00e9" + filler["value"][1:]
        self.assertEqual(routine.encoded_bytes(step), routine.MAX_STEP_VIEW_BYTES + 1)
        self.assertIsNone(routine.canonical_step(step, 7))
        lone = {"position": 1, "assistant": "a", "action": "b", "read_only": True, "inputs": [], "stored_inputs": []}
        lone["inputs"].append({"member": "m\ud800", "source": "literal", "value": "1"})
        self.assertIsNone(routine.canonical_step(lone, 1))
        # A literal's preview escapes a lone surrogate, so every projection Team makes stays encodable.
        self.assertEqual(routine.literal_preview("a\ud800"), '"a\\ud800"')

    def test_a_page_of_the_largest_steps_fits_its_bound_and_the_api_cap(self) -> None:
        steps = [_bounded_step(position) for position in range(1, 4)]
        page = {
            "routine_id": "0" * 32,
            "revision": 2**31 - 1,
            "plan_digest": "sha256:" + "f" * 64,
            "total": routine.MAX_ROUTINE_STEPS,
            "offset": 0,
            "steps": steps,
            "next": 3,
        }
        self.assertEqual(routine.canonical_page(page), page)
        # Within the Local API's 128 KiB response cap, with its trace id.
        self.assertLess(routine.encoded_bytes({**page, "trace_id": "f" * 32}), 128 * 1024)
        self.assertIsNone(routine.canonical_page({**page, "steps": [*steps, _bounded_step(4)], "next": 4}))

    def test_a_created_notice_of_the_largest_summary_fits_a_batch_many_times(self) -> None:
        notice = {
            "team_id": "t" * 40,
            "notice_id": "0" * 32,
            "version": 1,
            "routine_id": "1" * 32,
            "name": WIDE * routine.MAX_ROUTINE_NAME_CHARS,
            "run_id": None,
            "outcome": "created",
            "created_at": "2026-10-05T09:00:00Z",
            "detail": {
                "name": WIDE * routine.MAX_ROUTINE_NAME_CHARS,
                "plan": _largest_summary(),
                "output": {"mode": "changes", "step": routine.MAX_ROUTINE_STEPS, "when": None},
                "schedule": {"kind": "weekly", "weekday": 6, "time": "23:59"},
                "timezone": "/".join(["Z" * 32] * 3),
                "state": "rehearsal",
                "permitted": {"total": routine.MAX_PERMITTED, "changes": routine.MAX_PERMITTED},
                "model": None,
                "allowance": 0,
            },
            "usage": None,
            "protection_lost": False,
        }
        self.assertLessEqual(routine.encoded_bytes(_largest_summary()), routine.MAX_SUMMARY_BYTES)
        notices = [{**notice, "notice_id": f"{index:032x}"} for index in range(20)]
        batch = {"notices": notices, "more": False}
        self.assertEqual(routine.canonical_notice_batch(batch), batch)

    def test_the_largest_run_notice_fits_a_batch_twice(self) -> None:
        """A completed decision run at every bound: its shown output, decision message, summary, and usage."""
        output = {
            "step": 1,
            "state": "shown",
            "value": {"kind": "fields", "fields": [], "omitted": 0},
            "truncated": True,
        }
        text = {"kind": "text", "value": "", "cut": True}
        room = routine.MAX_OUTPUT_BYTES - routine.encoded_bytes({**output, "value": {**output["value"], "fields": []}})
        fields = []
        while True:
            label = f"k{len(fields):02d}"
            node = {**text, "value": WIDE * routine.MAX_OUTPUT_TEXT_CHARS}
            if routine.encoded_bytes([label, node]) + 1 > room or len(fields) == routine.MAX_OUTPUT_FIELDS:
                break
            fields.append([label, node])
            room -= routine.encoded_bytes([label, node]) + 1
        output["value"]["fields"] = fields
        self.assertIsNotNone(routine.canonical_output(output))
        models = [
            {"provider": f"p{index:02d}" + "p" * 61, "model": "m" * 64, "input_tokens": 10**9, "output_tokens": 10**9}
            for index in range(payload.MAX_TURN_USAGE_MODELS)
        ]
        notice = {
            "team_id": "t" * 40,
            "notice_id": "0" * 32,
            "version": 2**31 - 1,
            "routine_id": "1" * 32,
            "name": WIDE * routine.MAX_ROUTINE_NAME_CHARS,
            "run_id": "0" * 32,
            "outcome": "done",
            "created_at": "2026-10-05T09:00:00Z",
            "detail": {
                "plan": _largest_summary(),
                "output": output,
                "decision": {
                    "state": "decided",
                    "code": None,
                    "message": WIDE * routine.MAX_DECISION_MESSAGE_CHARS,
                },
            },
            "usage": {"duration_ms": payload.MAX_TURN_DURATION_MS, "models": models},
            "protection_lost": True,
        }
        batch = {"notices": [notice], "more": True}
        self.assertEqual(routine.canonical_notice_batch(batch), batch)
        self.assertLess(2 * routine.encoded_bytes(notice), routine.MAX_NOTICE_BATCH_BYTES)

    def test_a_run_page_with_the_largest_decision_record_fits_the_api_cap(self) -> None:
        models = [
            {"provider": f"p{index:02d}" + "p" * 61, "model": "m" * 64, "input_tokens": 10**9, "output_tokens": 10**9}
            for index in range(payload.MAX_TURN_USAGE_MODELS)
        ]
        record = {
            "state": "decided",
            "code": None,
            "model": {"provider": "anthropic", "model": "m" * 64, "effort": "medium"},
            "rules": [WIDE * routine.MAX_DECISION_RULE_CHARS] * routine.MAX_DECISION_RULES,
            "rationale": WIDE * routine.MAX_DECISION_RATIONALE_CHARS,
            "notify": True,
            "usage": {"duration_ms": payload.MAX_TURN_DURATION_MS, "models": models},
        }
        self.assertEqual(routine.canonical_decision_record(record), record)
        self.assertLessEqual(routine.encoded_bytes(record), routine.MAX_DECISION_RECORD_BYTES)
        envelope = 4 * 1024
        self.assertLess(routine.MAX_PAGE_BYTES + routine.MAX_DECISION_RECORD_BYTES + envelope, 128 * 1024)

    def test_a_list_at_every_bound_fits_its_allowance(self) -> None:
        name = WIDE * routine.MAX_ROUTINE_NAME_CHARS
        view = {
            "routine_id": "0" * 32,
            "name": name,
            "plan": _largest_summary(),
            "output": {"mode": "changes", "step": routine.MAX_ROUTINE_STEPS, "when": None},
            "schedule": {
                "kind": "continuous",
                "gap": routine.MAX_CONTINUOUS_GAP_SECONDS,
                "cap": routine.MAX_DAILY_RUNS,
            },
            "timezone": "/".join(["Z" * 32] * 3),
            "assistant_ids": sorted(f"a{index:02d}" + "a" * 37 for index in range(routine.MAX_NOTICE_ASSISTANTS)),
            "next_run_at": "2026-10-05T09:00:00Z",
            "needs_reconfirm": False,
            "deleting": False,
            "state": "rehearsal",
            "permitted": {"total": routine.MAX_PERMITTED, "changes": routine.MAX_PERMITTED},
            "permissions_revision": 2**31 - 1,
            "model": None,
            "allowance": 0,
        }
        decide = {
            **view,
            "plan": {**_largest_summary(), "steps": 192, "actions": [[ASSISTANT, ACTION, 192]]},
            "output": {"mode": "decide", "step": None, "when": "changes"},
            "model": {"provider": "anthropic", "model": "m" * 64, "effort": "medium"},
            "allowance": routine.MAX_ALLOWANCE,
        }
        run = {
            "run_id": "1" * 32,
            "routine_id": "0" * 32,
            "status": "frozen",
            "scheduled_at": "2026-10-05T09:00:00Z",
            "request_kind": "permission",
            "assistant_id": ASSISTANT,
            "action": ACTION,
            "position": {"phase": "decision", "call": routine.MAX_DECISION_CALLS},
            "steps": routine.MAX_ROUTINE_STEPS,
        }
        incident = {
            "incident_id": "2" * 32,
            "routine_id": "0" * 32,
            "name": name,
            "created_at": "2026-10-05T09:00:00Z",
            "assistant_id": ASSISTANT,
            "action": ACTION,
            "position": {"phase": "replay", "step": routine.MAX_ROUTINE_STEPS},
            "steps": routine.MAX_ROUTINE_STEPS,
        }
        listed = {
            "team_id": "t" * 40,
            "routines": [view, decide] * (routine.MAX_ROUTINES // 2),
            "runs": [run] * routine.MAX_ROUTINES,
            "incidents": [incident] * routine.MAX_UNRESOLVED_INCIDENTS,
            "trace_id": "f" * 32,
        }
        self.assertEqual(routine.canonical_routine_view(view), view)
        self.assertEqual(routine.canonical_routine_view(decide), decide)
        self.assertEqual(routine.canonical_run_view(run), run)
        self.assertEqual(routine.canonical_incident_view(incident), incident)
        self.assertLessEqual(routine.encoded_bytes(listed), routine.MAX_ROUTINE_LIST_BYTES)

    def test_the_largest_card_fits_the_terminal_line_with_its_reply_and_usage(self) -> None:
        """A card at its bound beside a 4,000-character reply in four-byte text stays under one NDJSON line."""
        from protocol.http.v1 import progress

        reply = WIDE * 4000
        self.assertLess(routine.MAX_PROPOSAL_BYTES + routine.encoded_bytes(reply) + 8 * 1024, progress.MAX_LINE_BYTES)


if __name__ == "__main__":
    unittest.main()
