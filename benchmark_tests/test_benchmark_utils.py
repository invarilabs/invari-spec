from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from benchmark_utils import (
    LIVE_DOC_APPROVAL_BENCHMARK_COMMAND,
    BenchmarkResultSummary,
    FakeBenchmarkLLMClient,
    classify_llm_prompt,
    compare_benchmark_summaries,
    summarize_benchmark_result,
)
from invari_spec.pipeline import (
    MarkdownToTlaRequest,
    build_dsl_fidelity_review_prompt,
    build_dsl_review_repair_prompt,
    build_dsl_variant_prompt,
    build_initial_markdown_to_dsl_prompt,
    build_minimal_dsl_repair_prompt,
    convert_markdown_to_tla,
    load_fixtures,
)


ROOT = Path(__file__).resolve().parents[1]
VALID_DSL = (ROOT / "examples" / "workflow_retry_with_fallback" / "expected.dsl.py").read_text(encoding="utf-8")
INVALID_DSL = '''
workflow("invalid_initial")

entity("task", Record(
    status=Enum("ready", "done"),
    retry_count=Int,
))

init(
    Eq(Field("task", "status"), "ready"),
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

REPAIRED_DSL = INVALID_DSL.replace(
    "init(\n",
    'init(\n    # FIX attempt 2: initialize missing field task.retry_count\n    Eq(Field("task", "retry_count"), 0),\n',
    1,
)


def structured_review(
    *,
    severity: str = "blocker",
    finding_id: str = "semantic_gap",
    required_change: str = "Update the DSL to cover the missing requirement.",
    choices: list[str] | None = None,
    selected_choice: str | None = None,
) -> str:
    finding = {
        "id": finding_id,
        "kind": "fidelity" if severity == "blocker" else "suggestion",
        "severity": severity,
        "lens": "outcome_correctness",
        "evidence": "The markdown contains a source-backed review finding.",
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


def fenced_json(payload: str) -> str:
    return f"```json\n{payload}\n```"


class BenchmarkUtilsTest(unittest.TestCase):
    def test_fake_llm_classifies_benchmark_prompt_types(self) -> None:
        markdown = "# Spec\nA task retries until success.\n"
        prompts = [
            build_initial_markdown_to_dsl_prompt(markdown, load_fixtures(), None),
            build_minimal_dsl_repair_prompt(
                markdown=markdown,
                previous_output="workflow('x')",
                validation_error="bad DSL",
                attempt_no=2,
            ),
            build_dsl_fidelity_review_prompt(markdown=markdown, dsl_source=VALID_DSL),
            build_dsl_review_repair_prompt(markdown=markdown, previous_output=VALID_DSL, review_feedback="1. Gap"),
            build_dsl_variant_prompt(markdown=markdown, previous_output=VALID_DSL, assumption_ledger=[]),
            "Summarize these modeling assumptions in natural language for a developer reviewing the generated spec artifacts.",
        ]

        self.assertEqual(
            [classify_llm_prompt(prompt) for prompt in prompts],
            [
                "initial_generation",
                "validation_repair",
                "fidelity_review",
                "fidelity_repair",
                "variant_generation",
                "assumptions_summary",
            ],
        )

    def test_summary_counts_llm_calls_timings_status_and_dsl_lines(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": VALID_DSL,
                    "fidelity_review": "No gaps found.",
                }
            )
            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                    generated_root=output_dir,
                    run_tlc=False,
                    cwd=ROOT,
                    collect_timings=True,
                ),
                llm_client=client,
            )

            summary = summarize_benchmark_result(result, llm_client=client)

            self.assertEqual(result.status, "pass")
            self.assertEqual(summary.total_llm_calls, 2)
            self.assertEqual(summary.llm_calls_by_kind["initial_generation"], 1)
            self.assertEqual(summary.fidelity_review_calls, 1)
            self.assertEqual(summary.fidelity_repair_calls, 0)
            self.assertEqual(summary.validation_repair_calls, 0)
            self.assertEqual(summary.final_status, "pass")
            self.assertEqual(summary.final_phase, "complete")
            self.assertEqual(summary.review_classification, "review_no_gaps")
            self.assertEqual(summary.convergence_classification, "converged")
            self.assertGreater(summary.stage_totals["dsl_generation"], 0.0)
            self.assertGreater(summary.stage_counts["file_io"], 0)
            self.assertGreater(summary.dsl_line_counts["initial"], 0)
            self.assertGreater(summary.dsl_line_counts["final"], 0)

            timing_only_summary = summarize_benchmark_result(result)
            self.assertEqual(timing_only_summary.total_llm_calls, 2)
            self.assertEqual(timing_only_summary.fidelity_review_calls, 1)

    def test_invalid_initial_dsl_reaches_validation_repair_before_fidelity_review(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": INVALID_DSL,
                    "validation_repair": REPAIRED_DSL,
                    "fidelity_review": "No gaps found.",
                }
            )
            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                    generated_root=output_dir,
                    max_attempts=2,
                    run_tlc=False,
                    cwd=ROOT,
                    collect_timings=True,
                ),
                llm_client=client,
            )

            summary = summarize_benchmark_result(result, llm_client=client)
            classifications = [call.classification for call in client.calls]

            self.assertEqual(result.status, "pass")
            self.assertEqual(classifications, ["initial_generation", "validation_repair", "fidelity_review"])
            self.assertEqual(summary.fidelity_review_calls, 1)
            self.assertEqual(summary.validation_repair_calls, 1)
            self.assertLess(classifications.index("validation_repair"), classifications.index("fidelity_review"))
            self.assertIsNone(result.attempts[0].review_feedback_path)
            self.assertTrue(result.attempts[0].validation_error_path)
            self.assertTrue(result.attempts[1].review_feedback_path)

    def test_suggestion_only_review_causes_zero_repair_calls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": VALID_DSL,
                    "fidelity_review": structured_review(severity="suggestion", finding_id="rename_for_clarity"),
                }
            )
            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                    generated_root=output_dir,
                    run_tlc=False,
                    cwd=ROOT,
                    collect_timings=True,
                ),
                llm_client=client,
            )

            summary = summarize_benchmark_result(result, llm_client=client)

            self.assertEqual(result.status, "pass")
            self.assertEqual(summary.fidelity_review_calls, 1)
            self.assertEqual(summary.fidelity_repair_calls, 0)
            self.assertEqual(result.review_summary.outcome if result.review_summary else None, "questions_or_suggestions_only")
            self.assertTrue((output_dir / "invari_spec_check" / "SPEC" / "review_attempts" / "attempt_1.findings.json").exists())
            self.assertFalse((output_dir / "invari_spec_check" / "SPEC" / "review_attempts" / "attempt_1.repair_response.txt").exists())

    def test_question_only_review_causes_zero_repair_calls(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": VALID_DSL,
                    "fidelity_review": structured_review(severity="question", finding_id="retry_limit_question"),
                }
            )
            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                    generated_root=output_dir,
                    run_tlc=False,
                    cwd=ROOT,
                    collect_timings=True,
                ),
                llm_client=client,
            )

            summary = summarize_benchmark_result(result, llm_client=client)

            self.assertEqual(result.status, "pass")
            self.assertEqual(summary.fidelity_repair_calls, 0)
            self.assertEqual(result.review_summary.outcome if result.review_summary else None, "questions_or_suggestions_only")

    def test_assumption_ledger_converges_after_selected_choice_is_carried_forward(self) -> None:
        review_with_choice = structured_review(
            finding_id="manual_review_gate",
            required_change="Apply the selected manual-review interpretation.",
            choices=["manual review is separate from approval", "manual review counts as approval"],
            selected_choice="manual review is separate from approval",
        )
        repair_response = f"```python\n{VALID_DSL}```\n```text\n```"
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": VALID_DSL,
                    "fidelity_review": [review_with_choice, "No gaps found."],
                    "fidelity_repair": repair_response,
                }
            )
            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                    generated_root=output_dir,
                    run_tlc=False,
                    cwd=ROOT,
                    collect_timings=True,
                ),
                llm_client=client,
            )

            summary = summarize_benchmark_result(result, llm_client=client)
            second_review_prompt = [call.prompt for call in client.calls if call.classification == "fidelity_review"][1]

            self.assertEqual(result.status, "pass")
            self.assertEqual(summary.fidelity_review_calls, 2)
            self.assertEqual(summary.fidelity_repair_calls, 1)
            self.assertEqual(len(result.review_summary.assumption_decisions if result.review_summary else []), 1)
            self.assertIn("manual review is separate from approval", second_review_prompt)
            self.assertTrue(
                (output_dir / "invari_spec_check" / "SPEC" / "review_attempts" / "assumption_ledger.json").exists()
            )

    def test_explore_mode_attempts_capped_variants(self) -> None:
        review_with_choices = structured_review(
            severity="question",
            finding_id="manual_review_gate",
            choices=["manual review before validation", "manual review after validation"],
            selected_choice="manual review before validation",
        )
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": VALID_DSL,
                    "fidelity_review": review_with_choices,
                    "variant_generation": [VALID_DSL, VALID_DSL],
                }
            )
            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                    generated_root=output_dir,
                    run_tlc=False,
                    cwd=ROOT,
                    collect_timings=True,
                    assumption_mode="explore",
                    explore_variant_limit=2,
                ),
                llm_client=client,
            )

            summary = summarize_benchmark_result(result, llm_client=client)
            report_path = output_dir / "invari_spec_check" / "SPEC" / "variants" / "variant_report.json"

            self.assertEqual(result.status, "pass")
            self.assertEqual(summary.llm_calls_by_kind["variant_generation"], 2)
            self.assertEqual(result.review_summary.variant_count if result.review_summary else None, 2)
            self.assertTrue(report_path.exists())

    def test_blocker_and_mixed_reviews_still_repair_only_for_blockers(self) -> None:
        mixed_review = json.dumps(
            {
                "verdict": "blockers_found",
                "findings": [
                    json.loads(structured_review(finding_id="missing_terminal_state"))["findings"][0],
                    json.loads(structured_review(severity="suggestion", finding_id="rename_for_clarity"))["findings"][0],
                ],
            }
        )
        repair_response = f"```python\n{VALID_DSL}```\n```text\n```"
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": VALID_DSL,
                    "fidelity_review": [mixed_review, "No gaps found."],
                    "fidelity_repair": repair_response,
                }
            )
            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                    generated_root=output_dir,
                    run_tlc=False,
                    cwd=ROOT,
                    collect_timings=True,
                ),
                llm_client=client,
            )

            repair_prompt = [call.prompt for call in client.calls if call.classification == "fidelity_repair"][0]

            self.assertEqual(result.status, "pass")
            self.assertEqual(result.review_summary.blocker_ids if result.review_summary else [], ["missing_terminal_state"])
            self.assertIn("missing_terminal_state", repair_prompt)
            self.assertNotIn("rename_for_clarity", repair_prompt)

    def test_malformed_review_fails_closed_without_repair(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": VALID_DSL,
                    "fidelity_review": "This is prose, not JSON.",
                }
            )
            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                    generated_root=output_dir,
                    run_tlc=False,
                    cwd=ROOT,
                    collect_timings=True,
                ),
                llm_client=client,
            )

            summary = summarize_benchmark_result(result, llm_client=client)

            self.assertEqual(result.status, "pass")
            self.assertEqual(summary.fidelity_repair_calls, 0)
            self.assertEqual(result.review_summary.outcome if result.review_summary else None, "review_parse_failed")
            self.assertTrue((output_dir / "invari_spec_check" / "SPEC" / "review_attempts" / "attempt_1.parse_failure.json").exists())

    def test_raw_and_fenced_json_review_outputs_parse(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": VALID_DSL,
                    "fidelity_review": fenced_json(structured_review(severity="suggestion", finding_id="document_naming")),
                }
            )
            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                    generated_root=output_dir,
                    run_tlc=False,
                    cwd=ROOT,
                    collect_timings=True,
                ),
                llm_client=client,
            )

            findings_payload = json.loads(
                (output_dir / "invari_spec_check" / "SPEC" / "review_attempts" / "attempt_1.findings.json").read_text(
                    encoding="utf-8"
                )
            )

            self.assertEqual(result.status, "pass")
            self.assertEqual(findings_payload["findings"][0]["id"], "document_naming")

    def test_non_converging_fidelity_review_is_capped_at_three_rounds(self) -> None:
        review_blocker = structured_review()
        repair_response = f"```python\n{VALID_DSL}```\n```text\n```"
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": VALID_DSL,
                    "fidelity_review": [review_blocker, review_blocker, review_blocker],
                    "fidelity_repair": [repair_response, repair_response, repair_response],
                }
            )
            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                    generated_root=output_dir,
                    run_tlc=False,
                    cwd=ROOT,
                    collect_timings=True,
                ),
                llm_client=client,
            )

            summary = summarize_benchmark_result(result, llm_client=client)
            review_and_repair_calls = summary.fidelity_review_calls + summary.fidelity_repair_calls

            self.assertEqual(result.status, "pass")
            self.assertEqual(summary.fidelity_review_calls, 3)
            self.assertEqual(summary.fidelity_repair_calls, 3)
            self.assertLessEqual(review_and_repair_calls, 6)
            self.assertGreaterEqual(20 - review_and_repair_calls, 14)
            self.assertTrue(any("cap of 3 rounds" in note for note in result.notes))
            self.assertTrue((output_dir / "invari_spec_check" / "SPEC" / "review_attempts" / "attempt_3.review.txt").exists())
            self.assertFalse((output_dir / "invari_spec_check" / "SPEC" / "review_attempts" / "attempt_4.review.txt").exists())

    def test_capped_invalid_review_repair_reports_cap_note(self) -> None:
        review_blocker = structured_review()
        valid_repair_response = f"```python\n{VALID_DSL}```\n```text\n```"
        invalid_repair_response = f"```python\n{INVALID_DSL}```\n```text\n```"
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": VALID_DSL,
                    "fidelity_review": [review_blocker, review_blocker, review_blocker],
                    "fidelity_repair": [valid_repair_response, valid_repair_response, invalid_repair_response],
                }
            )
            result = convert_markdown_to_tla(
                MarkdownToTlaRequest(
                    input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                    generated_root=output_dir,
                    max_attempts=5,
                    run_tlc=False,
                    cwd=ROOT,
                    collect_timings=True,
                ),
                llm_client=client,
            )

            summary = summarize_benchmark_result(result, llm_client=client)

            self.assertEqual(result.status, "error")
            self.assertEqual(result.phase, "dsl_validation")
            self.assertEqual(summary.fidelity_review_calls, 3)
            self.assertEqual(summary.fidelity_repair_calls, 3)
            self.assertTrue(any("cap of 3 rounds" in note for note in result.notes))

    def test_capped_tla_lowering_error_reports_cap_note(self) -> None:
        review_blocker = structured_review()
        repair_response = f"```python\n{VALID_DSL}```\n```text\n```"
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            client = FakeBenchmarkLLMClient(
                {
                    "initial_generation": VALID_DSL,
                    "fidelity_review": [review_blocker, review_blocker, review_blocker],
                    "fidelity_repair": [repair_response, repair_response, repair_response],
                }
            )
            with patch("invari_spec.pipeline.markdown_to_dsl.write_tla_artifacts", side_effect=RuntimeError("boom")):
                result = convert_markdown_to_tla(
                    MarkdownToTlaRequest(
                        input_path=ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md",
                        generated_root=output_dir,
                        run_tlc=False,
                        cwd=ROOT,
                        collect_timings=True,
                    ),
                    llm_client=client,
                )

            self.assertEqual(result.status, "error")
            self.assertEqual(result.phase, "tla_lowering")
            self.assertTrue(any("cap of 3 rounds" in note for note in result.notes))

    def test_compare_benchmark_summaries_reports_before_after_deltas(self) -> None:
        before = BenchmarkResultSummary(
            total_llm_calls=3,
            llm_calls_by_kind={
                "initial_generation": 1,
                "validation_repair": 1,
                "fidelity_review": 1,
                "fidelity_repair": 0,
                "assumptions_summary": 0,
                "unknown": 0,
            },
            fidelity_review_calls=1,
            fidelity_repair_calls=0,
            validation_repair_calls=1,
            stage_totals={"dsl_generation": 1.0, "repair_loop": 2.0},
            stage_counts={"dsl_generation": 1, "repair_loop": 1},
            final_status="pass",
            final_phase="complete",
            review_classification="review_no_gaps",
            convergence_classification="converged",
            dsl_line_counts={"initial": 12, "final": 15},
        )
        after = BenchmarkResultSummary(
            total_llm_calls=2,
            llm_calls_by_kind={
                "initial_generation": 1,
                "validation_repair": 0,
                "fidelity_review": 1,
                "fidelity_repair": 0,
                "assumptions_summary": 0,
                "unknown": 0,
            },
            fidelity_review_calls=1,
            fidelity_repair_calls=0,
            validation_repair_calls=0,
            stage_totals={"dsl_generation": 0.7, "fidelity_review": 0.5},
            stage_counts={"dsl_generation": 1, "fidelity_review": 1},
            final_status="pass",
            final_phase="complete",
            review_classification="review_no_gaps",
            convergence_classification="converged",
            dsl_line_counts={"initial": 11, "final": 14},
        )

        comparison = compare_benchmark_summaries(before, after)

        self.assertEqual(comparison.total_llm_calls_delta, -1)
        self.assertEqual(comparison.llm_call_deltas_by_kind["validation_repair"], -1)
        self.assertAlmostEqual(comparison.stage_total_deltas["dsl_generation"], -0.3)
        self.assertAlmostEqual(comparison.stage_total_deltas["repair_loop"], -2.0)
        self.assertEqual(comparison.stage_count_deltas["fidelity_review"], 1)
        self.assertEqual(comparison.dsl_line_count_deltas["initial"], -1)
        self.assertFalse(comparison.status_changed)
        self.assertFalse(comparison.phase_changed)

    def test_benchmark_command_help_exposes_live_doc_approval_command(self) -> None:
        proc = subprocess.run(
            [sys.executable, "-m", "invari_spec.cli", "benchmark", "--help"],
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("source .env", proc.stdout)
        self.assertIn("test_md/doc_approval.md", proc.stdout)
        self.assertEqual(LIVE_DOC_APPROVAL_BENCHMARK_COMMAND.count("test_md/doc_approval.md"), 1)

    def test_benchmark_command_resume_mode_prints_grouped_totals_and_counts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            proc = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "invari_spec.cli",
                    "benchmark",
                    str(ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md"),
                    "--dsl-file",
                    str(ROOT / "examples" / "workflow_retry_with_fallback" / "expected.dsl.py"),
                    "--output-dir",
                    str(Path(td) / "generated"),
                    "--no-run-tlc",
                ],
                cwd=ROOT,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("Grouped stage totals:", proc.stdout)
            self.assertIn("LLM/timing counts:", proc.stdout)
            self.assertIn("fidelity review calls: 0", proc.stdout)
            self.assertIn("final DSL lines:", proc.stdout)
