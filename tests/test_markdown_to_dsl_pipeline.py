from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from invari_spec.semantic_dsl import build_cfg, lower_to_tla, parse_dsl_file
from invari_spec.pipeline import (
    AssumptionDecision,
    MarkdownToTlaRequest,
    build_dsl_fidelity_review_prompt,
    build_dsl_review_repair_prompt,
    build_initial_markdown_to_dsl_prompt,
    build_minimal_dsl_repair_prompt,
    convert_markdown_to_tla,
    load_fixtures,
    render_result,
    normalize_common_dsl_syntax,
    normalize_llm_dsl_output,
)
from invari_spec.pipeline.markdown_to_dsl import _find_tla_jar, parse_structured_review_feedback


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "examples"


class FakeLLMClient:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []
        self._lock = threading.Lock()

    def generate(self, prompt: str, model: str | None = None, max_tokens: int = 16384) -> str:
        _ = model
        _ = max_tokens
        with self._lock:
            self.prompts.append(prompt)
            if not self.outputs:
                return ""
            return self.outputs.pop(0)


VALID_DSL = '''
workflow("repair_success")

entity("task", Record(
    status=Enum("ready", "done"),
    retry_count=Int,
))

init(
    Eq(Field("task", "status"), "ready"),
    # FIX attempt 2: initialized missing field task.retry_count
    Eq(Field("task", "retry_count"), 0),
)

action(
    "finish",
    requires=[
        Eq(Field("task", "status"), "ready"),
    ],
    changes=[
        SetField("task", "status", "done"),
    ],
)
'''


INVALID_DSL = VALID_DSL.replace(
    "    # FIX attempt 2: initialized missing field task.retry_count\n    Eq(Field(\"task\", \"retry_count\"), 0),\n",
    "",
)


VALID_DSL_WITHOUT_FIX_COMMENT = VALID_DSL.replace(
    "    # FIX attempt 2: initialized missing field task.retry_count\n",
    "",
)


INVALID_DSL_WITH_FIX_COMMENT = INVALID_DSL.replace(
    "init(\n",
    'init(\n    # FIX attempt 2: tried to repair but task.retry_count is still missing\n',
    1,
)


VALID_DSL_WITH_STATUS_REOPENED = VALID_DSL.replace(
    'status=Enum("ready", "done")',
    'status=Enum("ready", "done", "reopened")',
)


INVALID_REVIEW_REPAIR_DSL = VALID_DSL_WITH_STATUS_REOPENED.replace(
    '    Eq(Field("task", "retry_count"), 0),\n',
    "",
)


VALID_REPAIR_AFTER_INVALID_REVIEW = INVALID_REVIEW_REPAIR_DSL.replace(
    "init(\n",
    'init(\n    # FIX attempt 3: restore missing field task.retry_count after review repair\n    Eq(Field("task", "retry_count"), 0),\n',
    1,
)


def structured_review(
    *,
    severity: str = "blocker",
    finding_id: str = "missing_retry_count_init",
    required_change: str = "Initialize task.retry_count.",
    choices: list[str] | None = None,
    selected_choice: str | None = None,
) -> str:
    finding = {
        "id": finding_id,
        "kind": "fidelity" if severity == "blocker" else "suggestion",
        "severity": severity,
        "lens": "state_exhaustiveness",
        "evidence": "The markdown requires a complete task state.",
        "required_change": required_change if severity == "blocker" else "",
    }
    if choices is not None:
        finding["choices"] = choices
    if selected_choice is not None:
        finding["selected_choice"] = selected_choice
    return json.dumps(
        {
            "verdict": "blockers_found" if severity == "blocker" else "questions_or_suggestions_only",
            "findings": [finding],
        }
    )


class SpecDebuggingMarkdownToTlaTest(unittest.TestCase):
    def test_find_tla_jar_prefers_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            jar_path = Path(td) / "tla2tools.jar"
            jar_path.write_text("placeholder", encoding="utf-8")
            self.assertEqual(_find_tla_jar(jar_path), jar_path.resolve())

    def test_pipeline_fixtures_are_complete_and_lowerable(self) -> None:
        case_dirs = sorted(path for path in FIXTURE_ROOT.iterdir() if path.is_dir())
        self.assertGreaterEqual(len(case_dirs), 4)
        for case_dir in case_dirs:
            md_path = case_dir / "SPEC.md"
            dsl_path = case_dir / "expected.dsl.py"
            self.assertTrue(md_path.exists(), str(md_path))
            self.assertTrue(dsl_path.exists(), str(dsl_path))
            model = parse_dsl_file(dsl_path)
            self.assertIn("---- MODULE", lower_to_tla(model))
            self.assertIn("SPECIFICATION Spec", build_cfg(model))

    def test_initial_prompt_uses_curated_fixtures(self) -> None:
        prompt = build_initial_markdown_to_dsl_prompt("# Spec: target\n", load_fixtures())

        self.assertIn("Return only the semantic DSL source", prompt)
        self.assertIn("workflow_retry_with_fallback", prompt)
        self.assertIn("missing_fallback", prompt)
        self.assertIn("infinite_retry", prompt)
        self.assertIn("unreachable_success", prompt)
        self.assertIn("# Spec: target", prompt)
        self.assertNotIn("payment_flow", prompt)

    def test_normalize_llm_dsl_output_accepts_plain_and_fenced_source(self) -> None:
        plain = normalize_llm_dsl_output('workflow("x")\ninit()\naction("a")\n')
        fenced = normalize_llm_dsl_output('```python\nworkflow("x")\ninit()\naction("a")\n```')

        self.assertEqual(plain, 'workflow("x")\ninit()\naction("a")\n')
        self.assertEqual(fenced, 'workflow("x")\ninit()\naction("a")\n')

    def test_normalize_common_dsl_syntax_fixes_var_update_patterns_globally(self) -> None:
        source = '''
changes=[
    Set(Var("order_exists"), True),
    Set(Var("payment_exists"), True),
]
requires=[
    Eq("order_exists", False),
    Not("payment_exists"),
]
'''

        normalized = normalize_common_dsl_syntax(source)

        self.assertIn('Set("order_exists", True)', normalized)
        self.assertIn('Set("payment_exists", True)', normalized)
        self.assertIn('Eq(Var("order_exists"), False)', normalized)
        self.assertIn('Not(Var("payment_exists"))', normalized)
        self.assertNotIn("Set(Var(", normalized)

    def test_normalize_common_dsl_syntax_fixes_forbidden_when_patterns(self) -> None:
        source = '''
forbidden("cannot_cancel_paid_order", And(
    Eq(Field("order", "status"), "paid"),
))
forbidden("cannot_retry_paid_order", when=when=And(
    Eq(Field("order", "status"), "paid"),
))
'''

        normalized = normalize_common_dsl_syntax(source)

        self.assertIn('forbidden("cannot_cancel_paid_order", when=And(', normalized)
        self.assertIn('forbidden("cannot_retry_paid_order", when=And(', normalized)
        self.assertNotIn("when=when=", normalized)

    def test_normalize_llm_dsl_output_rejects_empty_and_prose(self) -> None:
        with self.assertRaises(ValueError):
            normalize_llm_dsl_output("")
        with self.assertRaises(ValueError):
            normalize_llm_dsl_output("Here is what I would do.")

    def test_minimal_repair_prompt_requires_preserving_previous_output_and_fix_comments(self) -> None:
        prompt = build_minimal_dsl_repair_prompt(
            markdown="# Spec: repair",
            previous_output=INVALID_DSL,
            validation_error="missing init values for: task.retry_count",
            attempt_no=2,
            warnings=["W_EXPLORATION_FROZEN_OUTCOME: payment.payment_succeeds is initialized in init"],
        )

        self.assertIn("Previous DSL output to repair", prompt)
        self.assertIn("missing init values for: task.retry_count", prompt)
        self.assertIn("Do not rewrite the whole file", prompt)
        self.assertIn("# FIX attempt 2:", prompt)
        self.assertIn("Exploration modeling warnings", prompt)
        self.assertIn('forbidden("name", when=predicate)', prompt)
        self.assertIn("Validator repair hints:", prompt)
        self.assertIn(INVALID_DSL.strip(), prompt)

    def test_fidelity_review_prompt_includes_required_lenses(self) -> None:
        prompt = build_dsl_fidelity_review_prompt(markdown="# Spec\n", dsl_source='workflow("x")\ninit()\n')

        self.assertIn("formal modeling reviewer", prompt)
        self.assertIn("outcome correctness", prompt)
        self.assertIn("entity/batch scope", prompt)
        self.assertIn('workflow("x")', prompt)
        self.assertIn('"verdict"', prompt)
        self.assertIn('"severity":"blocker | question | suggestion"', prompt)
        self.assertIn("Do not include prose outside the JSON", prompt)
        self.assertIn("Binding assumption ledger", prompt)

    def test_fidelity_review_prompt_carries_assumption_ledger(self) -> None:
        prompt = build_dsl_fidelity_review_prompt(
            markdown="# Spec\n",
            dsl_source='workflow("x")\ninit()\n',
            assumption_ledger=[
                AssumptionDecision(
                    id="A1",
                    finding_id="manual_review_gate",
                    source_attempt=1,
                    lens="state_exhaustiveness",
                    evidence="Manual review semantics are underspecified.",
                    choices=["manual review is separate from approval", "manual review counts as approval"],
                    selected_choice="manual review is separate from approval",
                )
            ],
        )

        self.assertIn("manual_review_gate", prompt)
        self.assertIn("manual review is separate from approval", prompt)
        self.assertIn("Do not reopen a ledger decision as a fresh blocker", prompt)

    def test_review_repair_prompt_requires_dsl_only_output(self) -> None:
        prompt = build_dsl_review_repair_prompt(
            markdown="# Spec\n",
            previous_output='workflow("x")\ninit()\n',
            review_feedback=structured_review(required_change="Change x."),
        )

        self.assertIn("Apply only the listed severity=blocker findings", prompt)
        self.assertIn("Do not repair question-only or suggestion-only findings", prompt)
        self.assertIn("Return exactly two fenced blocks", prompt)
        self.assertIn("Begin your response with the ```python fenced block", prompt)
        self.assertIn("Do not include any prose before the first fence", prompt)
        self.assertIn("still return the previous DSL unchanged", prompt)
        self.assertIn("```python", prompt)
        self.assertIn("```text", prompt)
        self.assertIn("Formal modeling review feedback:", prompt)
        self.assertIn('"required_change": "Change x."', prompt)
        self.assertIn("Binding assumption ledger", prompt)

    def test_assumption_ledger_is_persisted_and_carried_to_next_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: ambiguous manual review\n", encoding="utf-8")
            client = FakeLLMClient(
                [
                    VALID_DSL,
                    structured_review(
                        finding_id="manual_review_gate",
                        required_change="Route manual review through the selected gate.",
                        choices=["manual review is separate from approval", "manual review counts as approval"],
                        selected_choice="manual review is separate from approval",
                    ),
                    f"```python\n{VALID_DSL}```\n```text\n```",
                    "No gaps found.",
                ]
            )

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    max_attempts=1,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            ledger_path = root / "generated" / "invari_spec_check" / "SPEC" / "review_attempts" / "assumption_ledger.json"
            ledger_payload = json.loads(ledger_path.read_text(encoding="utf-8"))

            self.assertEqual(result.status, "pass")
            self.assertTrue(ledger_path.exists())
            self.assertEqual(ledger_payload["decisions"][0]["finding_id"], "manual_review_gate")
            self.assertEqual(ledger_payload["decisions"][0]["selected_choice"], "manual review is separate from approval")
            self.assertIn("manual review is separate from approval", client.prompts[2])
            self.assertIn("manual review is separate from approval", client.prompts[3])
            self.assertEqual(len(result.review_summary.assumption_decisions if result.review_summary else []), 1)
            self.assertEqual(result.review_summary.assumption_ledger_path if result.review_summary else None, str(ledger_path))

    def test_review_choice_selected_choice_can_extend_declared_choices(self) -> None:
        review = structured_review(
            choices=["path A", "path B"],
            selected_choice="path C",
        )

        _, findings = parse_structured_review_feedback(review)

        self.assertIn("path C", findings[0].choices)
        self.assertEqual(findings[0].selected_choice, "path C")

    def test_non_blocking_assumption_finding_may_omit_required_change(self) -> None:
        review = json.dumps(
            {
                "verdict": "questions_or_suggestions_only",
                "findings": [
                    {
                        "id": "manual_review_gate",
                        "kind": "assumption",
                        "severity": "question",
                        "lens": "state_exhaustiveness",
                        "evidence": "Manual review semantics are underspecified.",
                        "choices": ["separate approval", "counts as approval"],
                        "selected_choice": "separate approval",
                    }
                ],
            }
        )

        _, findings = parse_structured_review_feedback(review)

        self.assertEqual(findings[0].required_change, "")
        self.assertEqual(findings[0].selected_choice, "separate approval")

    def test_lens_value_in_review_kind_is_normalized_to_fidelity(self) -> None:
        review = json.dumps(
            {
                "verdict": "blockers_found",
                "findings": [
                    {
                        "id": "validation_gap",
                        "kind": "outcome_correctness",
                        "severity": "blocker",
                        "lens": "invariant_scoping",
                        "evidence": "The DSL skips a required validation gate.",
                        "required_change": "Add the missing validation gate.",
                    }
                ],
            }
        )

        _, findings = parse_structured_review_feedback(review)

        self.assertEqual(findings[0].kind, "fidelity")

    def test_review_loop_stops_when_no_gaps_found_is_in_longer_feedback(self) -> None:
        repaired = VALID_DSL.replace("# FIX attempt 2: initialized missing field task.retry_count\n", "")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient(
                [
                    repaired,
                    structured_review(),
                    f"```python\n{repaired.lstrip()}```\n```text\nquestion#1: What should task.retry_count start at?\nanswer#1: Initialize task.retry_count to 0.\n```",
                    "Review complete. No gaps found. Ready for validation.",
                ]
            )

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    max_attempts=1,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            self.assertEqual(result.status, "pass")
            self.assertEqual(result.attempt_count, 2)
            self.assertEqual(len(client.prompts), 4)
            self.assertEqual(
                Path(result.attempts[1].review_feedback_path or "").read_text(encoding="utf-8").strip(),
                "Review complete. No gaps found. Ready for validation.",
            )

    def test_repair_success_writes_attempts_fix_comments_and_tla(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient([INVALID_DSL, VALID_DSL, "No gaps found."])

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    max_attempts=2,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            self.assertEqual(result.status, "pass")
            self.assertEqual(result.attempt_count, 2)
            self.assertTrue(result.dsl_path and Path(result.dsl_path).exists())
            self.assertTrue(result.tla_path and Path(result.tla_path).exists())
            self.assertTrue(result.cfg_path and Path(result.cfg_path).exists())
            self.assertEqual(Path(result.dsl_path or "").name, "final.dsl.py")
            self.assertTrue((root / "generated" / "invari_spec_check" / "SPEC" / "initial.dsl.py").exists())
            self.assertIn("/dsl_validation_attempts/attempt_1.dsl.py", result.attempts[0].candidate_path or "")
            self.assertIn("# FIX attempt 2: initialized missing field task.retry_count", result.fix_comments)
            self.assertEqual(result.warnings, [])
            self.assertFalse(result.fairness_sensitive)
            self.assertEqual(result.liveness_classification, "not_applicable")
            self.assertEqual(len(client.prompts), 3)
            self.assertIsNone(result.attempts[0].review_feedback_path)
            self.assertTrue(result.attempts[1].review_feedback_path)
            self.assertTrue(Path(result.attempts[1].review_feedback_path or "").exists())
            self.assertIn("/review_attempts/attempt_1.review.txt", result.attempts[1].review_feedback_path or "")
            self.assertEqual(Path(result.attempts[1].review_feedback_path or "").read_text(encoding="utf-8").strip(), "No gaps found.")
            self.assertIsNone(result.attempts[1].review_repair_path)
            self.assertIsNone(result.attempts[1].assumptions_path)

    def test_repair_attempt_without_fix_comment_is_rejected_before_later_success(self) -> None:
        valid_attempt_3 = VALID_DSL.replace("# FIX attempt 2:", "# FIX attempt 3:")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient([INVALID_DSL, VALID_DSL_WITHOUT_FIX_COMMENT, valid_attempt_3, "No gaps found."])

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    max_attempts=3,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            self.assertEqual(result.status, "pass")
            self.assertEqual(result.attempt_count, 3)
            self.assertEqual(result.attempts[1].status, "invalid")
            self.assertIn("missing # FIX attempt 2", result.attempts[1].validation_error or "")
            self.assertIn("# FIX attempt 3: initialized missing field task.retry_count", result.fix_comments)

    def test_failure_after_max_attempts_preserves_candidates_and_writes_no_tla(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient([INVALID_DSL, INVALID_DSL])

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    max_attempts=2,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            self.assertEqual(result.status, "error")
            self.assertEqual(result.phase, "dsl_validation")
            self.assertEqual(result.attempt_count, 2)
            self.assertIsNone(result.tla_path)
            for attempt in result.attempts:
                self.assertTrue(attempt.candidate_path and Path(attempt.candidate_path).exists())
                self.assertTrue(attempt.validation_error_path and Path(attempt.validation_error_path).exists())
                self.assertIn("/dsl_validation_attempts/", attempt.candidate_path or "")
                self.assertIn("/dsl_validation_attempts/", attempt.validation_error_path or "")

    def test_stops_before_attempt_three_when_first_two_errors_match(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient([INVALID_DSL, INVALID_DSL_WITH_FIX_COMMENT, VALID_DSL])

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    max_attempts=3,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            self.assertEqual(result.status, "error")
            self.assertEqual(result.attempt_count, 2)
            self.assertEqual(len(client.prompts), 2)
            self.assertIn("missing init(...) values for: task.retry_count", result.attempts[0].validation_error or "")
            self.assertIn("missing init(...) values for: task.retry_count", result.attempts[1].validation_error or "")
            self.assertIsNone(result.tla_path)

    def test_review_feedback_loop_validates_repaired_candidate_before_finalization(self) -> None:
        repaired = VALID_DSL.replace("# FIX attempt 2: initialized missing field task.retry_count\n", "")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient(
                [
                    repaired,
                    structured_review(),
                    f"```python\n{repaired.lstrip()}```\n```text\nquestion#1: What should task.retry_count start at?\nanswer#1: Default task.retry_count to 0 because the spec never defines another initial value.\n```",
                    "No gaps found.",
                ]
            )

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    max_attempts=1,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            self.assertEqual(result.status, "pass")
            self.assertEqual(result.attempt_count, 2)
            self.assertEqual(len(client.prompts), 4)
            self.assertTrue(result.attempts[1].review_feedback_path)
            self.assertTrue(result.attempts[1].review_repair_path)
            self.assertTrue(result.attempts[1].assumptions_path)
            self.assertTrue((root / "generated" / "invari_spec_check" / "SPEC" / "initial.dsl.py").exists())
            self.assertTrue((root / "generated" / "invari_spec_check" / "SPEC" / "review_attempts" / "attempt_2.review.txt").exists())
            self.assertEqual(
                Path(result.attempts[1].review_feedback_path or "").read_text(encoding="utf-8").strip(),
                "No gaps found.",
            )
            self.assertEqual(
                Path(result.attempts[1].review_repair_path or "").read_text(encoding="utf-8"),
                repaired.lstrip("\n"),
            )
            self.assertTrue((root / "generated" / "invari_spec_check" / "SPEC" / "review_attempts" / "attempt_1.repair_response.txt").exists())
            self.assertEqual(
                Path(result.attempts[1].assumptions_path or "").read_text(encoding="utf-8").strip(),
                "attempt #1\nquestion#1: What should task.retry_count start at?\nanswer#1: Default task.retry_count to 0 because the spec never defines another initial value.",
            )
            self.assertIn("/review_attempts/attempt_1.dsl.py", result.attempts[1].review_repair_path or "")
            self.assertIn("/dsl_validation_attempts/attempt_2.dsl.py", result.attempts[1].candidate_path or "")

    def test_review_loop_with_no_questions_skips_assumptions_entry(self) -> None:
        repaired = VALID_DSL.replace("# FIX attempt 2: initialized missing field task.retry_count\n", "")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient(
                [
                    repaired,
                    structured_review(),
                    f"```python\n{repaired.lstrip()}```\n```text\n```",
                    "No gaps found.",
                ]
            )

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    max_attempts=1,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            self.assertEqual(result.status, "pass")
            self.assertIsNone(result.attempts[1].assumptions_path)
            self.assertFalse((root / "generated" / "invari_spec_check" / "SPEC" / "review_attempts" / "assumptions.txt").exists())

    def test_review_counter_is_monotonic_after_invalid_review_repair(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient(
                [
                    VALID_DSL,
                    structured_review(finding_id="add_reopened_status", required_change="Add reopened status."),
                    f"```python\n{INVALID_REVIEW_REPAIR_DSL.lstrip()}```\n```text\n```",
                    VALID_REPAIR_AFTER_INVALID_REVIEW,
                    "No gaps found.",
                ]
            )

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    max_attempts=3,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            review_dir = root / "generated" / "invari_spec_check" / "SPEC" / "review_attempts"
            self.assertEqual(result.status, "pass")
            self.assertEqual(result.attempt_count, 3)
            self.assertIn("add_reopened_status", (review_dir / "attempt_1.review.txt").read_text(encoding="utf-8"))
            self.assertTrue((review_dir / "attempt_1.dsl.py").exists())
            self.assertEqual((review_dir / "attempt_2.review.txt").read_text(encoding="utf-8").strip(), "No gaps found.")
            self.assertFalse((review_dir / "attempt_2.dsl.py").exists())
            self.assertIn("/review_attempts/attempt_1.review.txt", result.attempts[1].review_feedback_path or "")
            self.assertIn("/review_attempts/attempt_2.review.txt", result.attempts[2].review_feedback_path or "")
            self.assertIn("/dsl_validation_attempts/attempt_2.dsl.py", result.attempts[1].candidate_path or "")
            self.assertIn("/dsl_validation_attempts/attempt_3.dsl.py", result.attempts[2].candidate_path or "")

    def test_review_repair_missing_dsl_block_fails_explicitly(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient(
                [
                    VALID_DSL.replace("# FIX attempt 2: initialized missing field task.retry_count\n", ""),
                    structured_review(),
                    "```text\nquestion#1: What should task.retry_count start at?\nanswer#1: Set it to 0.\n```",
                ]
            )

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    max_attempts=1,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            self.assertEqual(result.status, "error")
            self.assertEqual(result.phase, "dsl_generation")
            self.assertIn("missing python fenced DSL block", result.validation_error or "")

    def test_malformed_review_feedback_fails_closed_without_repair(self) -> None:
        repaired = VALID_DSL.replace("# FIX attempt 2: initialized missing field task.retry_count\n", "")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient(
                [
                    repaired,
                    "1. Missing init for task.retry_count\nQuestions:\nquestion#1: What should task.retry_count start at?",
                ]
            )

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    max_attempts=1,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            self.assertEqual(result.status, "pass")
            self.assertEqual(len(client.prompts), 2)
            self.assertIsNone(result.attempts[0].review_repair_path)
            self.assertEqual(result.review_summary.outcome if result.review_summary else None, "review_parse_failed")
            self.assertTrue((root / "generated" / "invari_spec_check" / "SPEC" / "review_attempts" / "attempt_1.parse_failure.json").exists())

    def test_tlc_run_spawns_assumptions_summary_artifact(self) -> None:
        repaired = VALID_DSL.replace("# FIX attempt 2: initialized missing field task.retry_count\n", "")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient(
                [
                    repaired,
                    structured_review(),
                    f"```python\n{repaired.lstrip()}```\n```text\nquestion#1: What should task.retry_count start at?\nanswer#1: Default task.retry_count to 0 because the spec never defines another initial value.\n```",
                    "No gaps found.",
                    "The model assumes task.retry_count starts at 0, which makes the finish path immediately well-defined.",
                ]
            )

            with patch("invari_spec.pipeline.markdown_to_dsl._run_tlc", return_value=("pass", 0, "TLC OK", "")):
                result = convert_markdown_to_tla(
                    MarkdownToTlaRequest(
                        input_path=skill,
                        generated_root=root / "generated",
                        max_attempts=1,
                        run_tlc=True,
                        cwd=root,
                    ),
                    llm_client=client,
                )

            self.assertEqual(result.status, "pass")
            summary_path = root / "generated" / "invari_spec_check" / "SPEC" / "review_attempts" / "assumptions_summary.txt"
            self.assertTrue(summary_path.exists())
            self.assertIn("task.retry_count starts at 0", summary_path.read_text(encoding="utf-8"))
            self.assertTrue(any("Assumptions summary:" in note for note in result.notes))

    def test_existing_dsl_file_skips_llm_and_writes_tla(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: resume\n", encoding="utf-8")
            dsl = root / "attempt_1.dsl.py"
            dsl.write_text(VALID_DSL, encoding="utf-8")
            client = FakeLLMClient([""])

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    dsl_file=dsl,
                    max_attempts=1,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=client,
            )

            self.assertEqual(result.status, "pass")
            self.assertEqual(result.attempt_count, 1)
            self.assertEqual(client.prompts, [])
            self.assertTrue(result.dsl_path and Path(result.dsl_path).exists())
            self.assertTrue(result.tla_path and Path(result.tla_path).exists())
            self.assertTrue(result.cfg_path and Path(result.cfg_path).exists())
            self.assertTrue((root / "generated" / "invari_spec_check" / "SPEC" / "initial.dsl.py").exists())
            self.assertIn("/dsl_validation_attempts/attempt_1.dsl.py", result.attempts[0].candidate_path or "")

    def test_resume_mode_preserves_exploration_warnings(self) -> None:
        warned_dsl = '''
workflow("warned_resume")

entity("payment", Record(
    status=Enum("pending", "success", "failed"),
    payment_succeeds=Bool,
))

init(
    Eq(Field("payment", "status"), "pending"),
    Eq(Field("payment", "payment_succeeds"), False),
)

action(
    "payment_attempt_succeeds",
    requires=[
        Eq(Field("payment", "status"), "pending"),
        Field("payment", "payment_succeeds"),
    ],
    changes=[
        SetField("payment", "status", "success"),
    ],
)

action(
    "payment_attempt_fails",
    requires=[
        Eq(Field("payment", "status"), "pending"),
        Not(Field("payment", "payment_succeeds")),
    ],
    changes=[
        SetField("payment", "status", "failed"),
    ],
)
'''
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: resume\n", encoding="utf-8")
            dsl = root / "attempt_1.dsl.py"
            dsl.write_text(warned_dsl, encoding="utf-8")

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    dsl_file=dsl,
                    max_attempts=1,
                    run_tlc=False,
                    cwd=root,
                ),
                llm_client=FakeLLMClient([""]),
            )

            self.assertEqual(result.status, "pass")
            self.assertTrue(any(w.startswith("W_EXPLORATION_FROZEN_OUTCOME") for w in result.warnings))
            self.assertTrue(result.underspecified_assumptions)

    def test_render_result_surfaces_public_classification_fields(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: resume\n", encoding="utf-8")
            dsl = root / "attempt_1.dsl.py"
            dsl.write_text(VALID_DSL, encoding="utf-8")

            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=skill,
                    generated_root=root / "generated",
                    dsl_file=dsl,
                    max_attempts=1,
                    run_tlc=False,
                    cwd=root,
                )
            )

            rendered = render_result(result, "text")
            self.assertIn("BUG_CLASSES:", rendered)
            self.assertIn("UNDERSPECIFIED:", rendered)
            self.assertIn("LIVENESS:", rendered)

    def test_render_result_surfaces_review_summary_fields(self) -> None:
        payload = {
            "status": "pass",
            "phase": "complete",
            "input_path": "SPEC.md",
            "attempt_count": 1,
            "bug_classes": [],
            "review_summary": {
                "outcome": "questions_or_suggestions_only",
                "review_rounds": 1,
                "repair_rounds": 0,
                "blocker_ids": [],
                "assumption_count": 0,
            },
            "summary": "ok",
        }

        rendered = render_result(payload, "text")

        self.assertIn("REVIEW_OUTCOME: questions_or_suggestions_only", rendered)
        self.assertIn("REVIEW_ROUNDS: 1", rendered)
        self.assertIn("REVIEW_REPAIR_ROUNDS: 0", rendered)
        self.assertIn("REVIEW_BLOCKERS: (none)", rendered)


if __name__ == "__main__":
    unittest.main()
