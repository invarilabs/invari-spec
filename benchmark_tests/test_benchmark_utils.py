from __future__ import annotations

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
            "Summarize these modeling assumptions in natural language for a developer reviewing the generated spec artifacts.",
        ]

        self.assertEqual(
            [classify_llm_prompt(prompt) for prompt in prompts],
            [
                "initial_generation",
                "validation_repair",
                "fidelity_review",
                "fidelity_repair",
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

    def test_non_converging_fidelity_review_is_capped_at_three_rounds(self) -> None:
        review_blocker = "1. Still missing a semantic requirement."
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
        review_blocker = "1. Still missing a semantic requirement."
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
        review_blocker = "1. Still missing a semantic requirement."
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
