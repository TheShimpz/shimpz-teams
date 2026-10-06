"""Team's HTTP protocol verifier refuses every drifted manifest, module, and golden vector."""

from __future__ import annotations

import types
import unittest
from pathlib import Path

from test_protocol_verifier_edges import HTTP, _execute, _rehash, _rewrite_json


class TeamHttpVerifierEdgeTests(unittest.TestCase):
    def _assert_vector_mutations_refused(
        self, *mutations: object, refusal: type[BaseException] | tuple[type[BaseException], ...]
    ) -> None:
        """The pinned protocol verifier refuses vectors.json after each one of these mutations."""
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(refusal):
                _execute(
                    HTTP / "verify.py", lambda root, mutation=mutate: _rewrite_json(root, "vectors.json", mutation)
                )

    def test_accepts_the_current_pinned_protocol(self) -> None:
        self.assertIn("golden vectors are valid", _execute(HTTP / "verify.py"))

    def test_rejects_manifest_inventory_digest_root_and_header_drift(self) -> None:
        def malformed_row(root: Path) -> None:
            manifest = root / "contract-files.sha256"
            manifest.write_text("invalid\n", encoding="ascii")

        def remove_row(root: Path) -> None:
            manifest = root / "contract-files.sha256"
            rows = manifest.read_text(encoding="ascii").splitlines()
            manifest.write_text("\n".join(rows[1:]) + "\n", encoding="ascii")

        mutations = (
            malformed_row,
            remove_row,
            lambda root: (root / "README.md").write_text("drift", encoding="utf-8"),
            lambda root: _rewrite_json(root, "vectors.json", lambda value: value.update({"version": 2})),
            lambda root: _rewrite_json(root, "vectors.json", lambda value: value.update({"headers": {}})),
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit):
                _execute(HTTP / "verify.py", mutate)

    def test_rejects_each_golden_vector_family_when_its_expected_outcome_drifts(self) -> None:
        def flip_case(section: str, *, valid: bool) -> object:
            def mutate(value: dict[str, object]) -> None:
                case = next(item for item in value[section] if item["valid"] is valid)
                case["valid"] = not valid

            return mutate

        mutations = (
            flip_case("frames", valid=True),
            flip_case("frames", valid=False),
            flip_case("human_response_frames", valid=True),
            flip_case("human_response_frames", valid=False),
            flip_case("chat_stream", valid=True),
            flip_case("chat_stream", valid=False),
            flip_case("chat_stream_lines", valid=True),
            flip_case("chat_stream_lines", valid=False),
        )
        self._assert_vector_mutations_refused(*mutations, refusal=SystemExit)

    def test_rejects_supervisor_and_identifier_vector_drift(self) -> None:
        def accepted_supervisor(value: dict[str, object]) -> None:
            value["local_supervisor"]["invalid"] = [value["local_supervisor"]["valid"][0]]

        def rejected_supervisor(value: dict[str, object]) -> None:
            value["local_supervisor"]["valid"] = [value["local_supervisor"]["invalid"][0]]

        def invalid_positive_identifier(value: dict[str, object]) -> None:
            value["identifiers"]["team"]["valid"] = ["Bad"]

        def valid_negative_identifier(value: dict[str, object]) -> None:
            value["identifiers"]["assistant"]["invalid"] = ["assistant"]

        self._assert_vector_mutations_refused(
            accepted_supervisor,
            rejected_supervisor,
            invalid_positive_identifier,
            valid_negative_identifier,
            refusal=(SystemExit, ValueError),
        )

    def test_rejects_missing_or_drifted_chat_conversation_vectors(self) -> None:
        def missing(value: dict[str, object]) -> None:
            value["chat_conversation"]["invalid"] = []

        def accepted_invalid(value: dict[str, object]) -> None:
            value["chat_conversation"]["invalid"] = [[]]

        def rejected_valid(value: dict[str, object]) -> None:
            value["chat_conversation"]["valid"] = [{"generated": "nine-entries"}]

        self._assert_vector_mutations_refused(missing, accepted_invalid, rejected_valid, refusal=SystemExit)

    def test_rejects_missing_or_drifted_clarification_label_vectors(self) -> None:
        def missing(root: Path) -> None:
            _rewrite_json(root, "vectors.json", lambda value: value["clarification_labels"].update({"composed": []}))

        def composed(root: Path) -> None:
            _rewrite_json(
                root,
                "vectors.json",
                lambda value: value["clarification_labels"]["composed"][0].update({"message": "drift"}),
            )

        def authored_segments(root: Path) -> None:
            _rewrite_json(
                root,
                "vectors.json",
                lambda value: value["clarification_labels"]["authored_segments"][0].update({"segments": []}),
            )

        def unlabelled_locale(root: Path) -> None:
            module = root / "payload.py"
            text = module.read_text(encoding="utf-8")
            module.write_text(text.replace('    "zh": {"question": "问题", "answer": "回答"},\n', ""), encoding="utf-8")
            _rehash(root, "payload.py")

        for mutate in (missing, composed, authored_segments, unlabelled_locale):
            with self.subTest(mutate=mutate.__name__), self.assertRaises(SystemExit):
                _execute(HTTP / "verify.py", mutate)

    def test_rejects_routine_answer_replies_that_miss_a_language_or_the_english_default(self) -> None:
        def edited(old: str, new: str):
            def mutate(root: Path) -> None:
                module = root / "routine_proposal.py"
                module.write_text(module.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
                _rehash(root, "routine_proposal.py")

            return mutate

        for mutate in (
            edited('    "zh": "已将你的回答应用到例行任务。",\n', ""),
            edited('ANSWER_REPLIES[locale or "en"]', 'ANSWER_REPLIES[locale or "pt"]'),
        ):
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit):
                _execute(HTTP / "verify.py", mutate)

    def test_rejects_missing_or_drifted_routine_phrase_vectors(self) -> None:
        def missing(value: dict[str, object]) -> None:
            value["routine_phrase"]["stated"] = []

        def drifted(value: dict[str, object]) -> None:
            value["routine_phrase"]["outputs"][0]["outputs"] = ["none"]

        def asks_drifted(value: dict[str, object]) -> None:
            value["routine_phrase"]["team_asks"][0]["asks"] = False

        def request_drifted(value: dict[str, object]) -> None:
            value["routine_phrase"]["requests_routine"][0]["requests"] = False

        for mutate in (missing, drifted, asks_drifted, request_drifted):
            with self.subTest(mutate=mutate.__name__), self.assertRaises(SystemExit):
                _execute(
                    HTTP / "verify.py", lambda root, mutation=mutate: _rewrite_json(root, "vectors.json", mutation)
                )

    def test_rejects_routine_output_choices_missing_a_language_or_naming_one_output_twice(self) -> None:
        def edited(old: str, new: str):
            def mutate(root: Path) -> None:
                module = root / "routine_proposal.py"
                module.write_text(module.read_text(encoding="utf-8").replace(old, new), encoding="utf-8")
                _rehash(root, "routine_proposal.py")

            return mutate

        for mutate in (
            edited('"none": "Não mostrar"', '"none": "Mostrar somente quando mudar"'),
            edited('    "zh": {"show": "每次运行都显示"', '    "xx": {"show": "每次运行都显示"'),
        ):
            with self.subTest(mutate=mutate), self.assertRaises(SystemExit):
                _execute(HTTP / "verify.py", mutate)

    def test_rejects_missing_or_drifted_rendered_copy_vectors(self) -> None:
        def missing(value: dict[str, object]) -> None:
            value["rendered_copy"]["invalid"] = []

        def accepted_invalid(value: dict[str, object]) -> None:
            value["rendered_copy"]["invalid"] = [value["rendered_copy"]["valid"][0]]

        def rejected_valid(value: dict[str, object]) -> None:
            value["rendered_copy"]["valid"] = [value["rendered_copy"]["invalid"][0]]

        self._assert_vector_mutations_refused(missing, accepted_invalid, rejected_valid, refusal=SystemExit)

    def test_rejects_missing_or_drifted_clarification_vectors(self) -> None:
        def missing(value: dict[str, object]) -> None:
            value["clarification"]["invalid"] = []

        def accepted_invalid(value: dict[str, object]) -> None:
            value["clarification"]["invalid"] = [value["clarification"]["valid"][0]]

        def rejected_valid(value: dict[str, object]) -> None:
            value["clarification"]["valid"] = [{**value["clarification"]["valid"][0], "extra": 1}]

        def drifted_rendering(value: dict[str, object]) -> None:
            value["clarification"]["rendered"][0] = "Something else"

        self._assert_vector_mutations_refused(
            missing, accepted_invalid, rejected_valid, drifted_rendering, refusal=SystemExit
        )

    def test_rejects_missing_or_drifted_skill_vectors(self) -> None:
        def missing(value: dict[str, object]) -> None:
            value["skills"]["invalid"] = []

        def accepted_invalid(value: dict[str, object]) -> None:
            value["skills"]["invalid"] = [value["skills"]["valid"][1]]

        def rejected_valid(value: dict[str, object]) -> None:
            value["skills"]["valid"] = [[{"key": "procedure-000000000000", "contracts": {}, "steps": []}]]

        def drifted_apply(value: dict[str, object]) -> None:
            value["knowledge_apply"][0]["result"]["skills"] = []

        self._assert_vector_mutations_refused(
            missing, accepted_invalid, rejected_valid, drifted_apply, refusal=SystemExit
        )

    def test_rejects_missing_or_drifted_memory_vectors(self) -> None:
        def missing(value: dict[str, object]) -> None:
            value["memory"]["valid"] = []

        def accepted_invalid(value: dict[str, object]) -> None:
            value["memory_changes"]["invalid"] = [value["memory_changes"]["valid"][1]]

        def rejected_valid(value: dict[str, object]) -> None:
            value["memory"]["valid"] = [[{"topic": "Bad Topic", "preference": "x"}]]

        def drifted_apply(value: dict[str, object]) -> None:
            value["memory_apply"][0]["result"] = []

        self._assert_vector_mutations_refused(
            missing, accepted_invalid, rejected_valid, drifted_apply, refusal=SystemExit
        )

    def test_rejects_missing_or_drifted_routine_vectors(self) -> None:
        def missing_schedules(value: dict[str, object]) -> None:
            value["routine_schedule"]["daily_rate"] = []

        def rejected_schedule(value: dict[str, object]) -> None:
            value["routine_schedule"]["valid"] = [{"kind": "daily", "time": "25:00"}]

        def accepted_schedule(value: dict[str, object]) -> None:
            value["routine_schedule"]["invalid"] = [{"kind": "daily", "time": "09:00"}]

        def drifted_rate(value: dict[str, object]) -> None:
            value["routine_schedule"]["daily_rate"][0]["rate"] = "5"

        def missing_timezones(value: dict[str, object]) -> None:
            value["routine_timezone"]["valid"] = []

        def rejected_timezone(value: dict[str, object]) -> None:
            value["routine_timezone"]["valid"] = ["../UTC"]

        def accepted_timezone(value: dict[str, object]) -> None:
            value["routine_timezone"]["invalid"] = ["UTC"]

        def missing_identities(value: dict[str, object]) -> None:
            value["chat_request_identity"]["valid"] = []

        def rejected_identity(value: dict[str, object]) -> None:
            value["chat_request_identity"]["valid"] = [{**value["chat_request_identity"]["valid"][0], "extra": 1}]

        def accepted_identity(value: dict[str, object]) -> None:
            value["chat_request_identity"]["invalid"] = [value["chat_request_identity"]["valid"][0]]

        def missing_routine_assertions(value: dict[str, object]) -> None:
            value["local_routine"]["invalid"] = []

        def rejected_routine_assertion(value: dict[str, object]) -> None:
            value["local_routine"]["valid"] = [{**value["local_routine"]["valid"][0], "authority": "session"}]

        def accepted_routine_assertion(value: dict[str, object]) -> None:
            value["local_routine"]["invalid"] = [value["local_routine"]["valid"][0]]

        mutations = (
            missing_routine_assertions,
            rejected_routine_assertion,
            accepted_routine_assertion,
            missing_schedules,
            rejected_schedule,
            accepted_schedule,
            drifted_rate,
            missing_timezones,
            rejected_timezone,
            accepted_timezone,
            missing_identities,
            rejected_identity,
            accepted_identity,
        )
        for mutate in mutations:
            with self.subTest(mutate=mutate.__name__), self.assertRaises(SystemExit):
                _execute(
                    HTTP / "verify.py", lambda root, mutation=mutate: _rewrite_json(root, "vectors.json", mutation)
                )

    def test_rejects_missing_or_drifted_routine_view_vectors(self) -> None:
        def missing_views(value: dict[str, object]) -> None:
            value["routine_views"].pop("claim")

        def rejected_view(value: dict[str, object]) -> None:
            value["routine_views"]["claim"]["valid"] = [{"run": None, "extra": 1}]

        def accepted_view(value: dict[str, object]) -> None:
            value["routine_views"]["claim"]["invalid"] = [{"run": None, "next_due_at": None}]

        for mutate in (missing_views, rejected_view, accepted_view):
            with self.subTest(mutate=mutate.__name__), self.assertRaises(SystemExit):
                _execute(
                    HTTP / "verify.py", lambda root, mutation=mutate: _rewrite_json(root, "vectors.json", mutation)
                )

    def test_rejects_missing_or_drifted_recorded_routine_vectors(self) -> None:
        def missing_positions(value: dict[str, object]) -> None:
            value["routine_position"]["invalid"] = []

        def rejected_position(value: dict[str, object]) -> None:
            value["routine_position"]["valid"] = [{"value": {"phase": "replay", "step": 2}, "steps": 1}]

        def accepted_position(value: dict[str, object]) -> None:
            value["routine_position"]["invalid"] = [{"value": {"phase": "decision", "call": 1}, "steps": 0}]

        def missing_generated(value: dict[str, object]) -> None:
            value["routine_proposal"]["generated"] = ["largest-unicode"]

        def drifted_largest(value: dict[str, object]) -> None:
            value["routine_proposal"]["valid"][0]["name"] = "x" * 81

        def missing_refusals(value: dict[str, object]) -> None:
            value.pop("routine_refusal")

        def rejected_record(value: dict[str, object]) -> None:
            value["routine_decision_record"]["valid"] = [{"state": "decided"}]

        def accepted_answer(value: dict[str, object]) -> None:
            value["routine_proposal_answer"]["invalid"] = [value["routine_proposal_answer"]["valid"][0]]

        for mutate in (
            missing_positions,
            rejected_position,
            accepted_position,
            missing_generated,
            drifted_largest,
            missing_refusals,
            rejected_record,
            accepted_answer,
        ):
            with self.subTest(mutate=mutate.__name__), self.assertRaises(SystemExit):
                _execute(
                    HTTP / "verify.py", lambda root, mutation=mutate: _rewrite_json(root, "vectors.json", mutation)
                )

    def test_rejects_missing_or_drifted_routine_diagnostics_vectors(self) -> None:
        def missing_diagnostics(value: dict[str, object]) -> None:
            value.pop("routine_diagnostics")

        def rejected_diagnostics(value: dict[str, object]) -> None:
            value["routine_diagnostics"]["valid"] = [{"team_id": "team_1", "run_id": "b" * 32}]

        def accepted_diagnostics(value: dict[str, object]) -> None:
            value["routine_diagnostics"]["invalid"] = [{"team_id": "team_1", "run_id": "b" * 32, "diagnostics": []}]

        for mutate in (missing_diagnostics, rejected_diagnostics, accepted_diagnostics):
            with self.subTest(mutate=mutate.__name__), self.assertRaises(SystemExit):
                _execute(
                    HTTP / "verify.py", lambda root, mutation=mutate: _rewrite_json(root, "vectors.json", mutation)
                )

    def test_rejects_a_positive_supervisor_vector_that_is_not_canonical(self) -> None:
        from protocol.http.v1 import supervisor

        fake = types.ModuleType("supervisor")
        fake.ASSERTION_HEADER = supervisor.ASSERTION_HEADER
        fake.SupervisorAssertionError = supervisor.SupervisorAssertionError
        fake.canonical_claims = lambda _value: {}
        with self.assertRaises(SystemExit):
            _execute(HTTP / "verify.py", modules={"supervisor": fake})

    def test_rejects_action_label_text_vector_drift(self) -> None:
        def missing_purpose(value: dict[str, object]) -> None:
            value["purpose"] = {"valid": [], "invalid": ["x"]}

        def rejected_help_url(value: dict[str, object]) -> None:
            value["help_url"]["valid"] = ["https://example.com"]

        def admitted_invalid_locale(value: dict[str, object]) -> None:
            value["chat_locale"]["invalid"] = ["en"]

        def admitted_invalid_turn_usage(value: dict[str, object]) -> None:
            value["turn_usage"]["invalid"] = [value["turn_usage"]["valid"][0]]

        def rejected_label(value: dict[str, object]) -> None:
            value["action_label_text"]["labels"] = [" padded "]

        def admitted_invalid_label(value: dict[str, object]) -> None:
            value["action_label_text"]["invalid_labels"] = ["Valid label"]

        self._assert_vector_mutations_refused(
            missing_purpose,
            rejected_help_url,
            admitted_invalid_locale,
            admitted_invalid_turn_usage,
            rejected_label,
            admitted_invalid_label,
            refusal=SystemExit,
        )


if __name__ == "__main__":
    unittest.main()
