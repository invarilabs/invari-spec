from __future__ import annotations

import argparse
from collections import Counter
import sys
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

from invari_spec.pipeline import MarkdownToTlaRequest, convert_markdown_to_tla, render_result


DOC_APPROVAL_LIVE_BENCHMARK_COMMAND = (
    "set -a; source .env; set +a; "
    "python3 -m invari_spec.cli benchmark test_md/doc_approval.md "
    "--output-dir .invari/tmp/INV-66/doc_approval"
)


def _add_pipeline_args(parser: argparse.ArgumentParser, *, file_required: bool) -> None:
    if file_required:
        parser.add_argument("--file", required=True, help="Path to the input spec markdown file.")
    else:
        parser.add_argument("file", help="Path to the input spec markdown file.")
    parser.add_argument("--dsl-file", help="Resume from an existing DSL attempt file.")
    parser.add_argument("--output-dir", default="generated", help="Directory for generated artifacts.")
    parser.add_argument("--model", help="Optional LLM model name.")
    parser.add_argument("--prompt-path", help="Optional replacement prompt file.")
    parser.add_argument("--tla-jar-path", help="Path to tla2tools.jar.")
    parser.add_argument("--max-attempts", type=int, default=3)
    parser.add_argument(
        "--assumption-mode",
        choices=("default", "explore", "off"),
        default="default",
        help="How review handles underspecified workflow choices.",
    )
    parser.add_argument("--explore-variant-limit", type=int, default=4, help="Maximum variants to generate in explore mode.")
    tlc_group = parser.add_mutually_exclusive_group()
    tlc_group.add_argument("--run-tlc", dest="run_tlc", action="store_true", default=True)
    tlc_group.add_argument("--no-run-tlc", dest="run_tlc", action="store_false")


def _request_from_args(args: argparse.Namespace) -> MarkdownToTlaRequest:
    return MarkdownToTlaRequest(
        input_path=Path(args.file),
        generated_root=Path(args.output_dir),
        llm_model=args.model,
        prompt_path=Path(args.prompt_path) if args.prompt_path else None,
        dsl_file=Path(args.dsl_file) if args.dsl_file else None,
        tla_jar_path=Path(args.tla_jar_path) if args.tla_jar_path else None,
        max_attempts=args.max_attempts,
        run_tlc=args.run_tlc,
        cwd=Path.cwd(),
        collect_timings=args.command == "benchmark",
        assumption_mode=args.assumption_mode,
        explore_variant_limit=args.explore_variant_limit,
    )


def _render_benchmark_result(result) -> str:
    stage_totals: dict[str, float] = {}
    stage_counts: Counter[str] = Counter()
    timing_rows: list[str] = []
    total = None
    sorted_timings = sorted(result.timings, key=lambda timing: (timing.stage, timing.detail or ""))
    for timing in sorted_timings:
        if timing.stage == "total":
            total = timing.seconds
            continue
        stage_totals[timing.stage] = stage_totals.get(timing.stage, 0.0) + timing.seconds
        stage_counts[timing.stage] += 1
        label = timing.stage.replace("_", " ")
        if timing.detail:
            label = f"{label} ({timing.detail})"
        timing_rows.append(f"- {label}: {timing.seconds:.3f}s")

    validation_repair_calls = sum(
        1 for timing in sorted_timings if timing.stage == "repair_loop" and (timing.detail or "").startswith("attempt ")
    )
    fidelity_repair_calls = sum(
        1
        for timing in sorted_timings
        if timing.stage == "repair_loop" and (timing.detail or "").startswith("review repair ")
    )
    final_dsl_lines = None
    if result.dsl_path and Path(result.dsl_path).exists():
        final_dsl_lines = len(Path(result.dsl_path).read_text(encoding="utf-8").splitlines())

    lines = [
        "Pipeline timing:",
        "Grouped stage totals:",
    ]
    for stage in sorted(stage_totals):
        label = stage.replace("_", " ")
        count = stage_counts[stage]
        suffix = "event" if count == 1 else "events"
        lines.append(f"- {label}: {stage_totals[stage]:.3f}s ({count} {suffix})")
    if total is not None:
        lines.append(f"- Total: {total:.3f}s")
    lines.extend(
        [
            "LLM/timing counts:",
            f"- initial generation calls: {stage_counts['dsl_generation']}",
            f"- fidelity review calls: {stage_counts['fidelity_review']}",
            f"- fidelity repair calls: {fidelity_repair_calls}",
            f"- validation repair calls: {validation_repair_calls}",
            f"- assumptions summary calls: {stage_counts['assumptions_summary']}",
            f"- attempts: {result.attempt_count}",
            f"- fix comments: {len(result.fix_comments)}",
        ]
    )
    if final_dsl_lines is not None:
        lines.append(f"- final DSL lines: {final_dsl_lines}")
    if timing_rows:
        lines.append("Timing rows:")
        lines.extend(timing_rows)
    lines.extend(
        [
            f"Status: {result.status}",
            f"Phase: {result.phase}",
            f"Run dir: {result.run_dir}",
        ]
    )
    if result.validation_error:
        lines.append(f"Validation error: {result.validation_error}")
    return "\n".join(lines)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invari-spec", description="Debug prose specs with executable checks.")
    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser("check", help="Run the markdown -> DSL -> TLA+ pipeline on a spec markdown file.")
    _add_pipeline_args(check, file_required=True)
    check.add_argument("--format", choices=("text", "json"), default="text")

    benchmark = subparsers.add_parser(
        "benchmark",
        help="Run the pipeline and print per-stage timing.",
        epilog=f"Live doc_approval benchmark with .env sourced: {DOC_APPROVAL_LIVE_BENCHMARK_COMMAND}",
    )
    _add_pipeline_args(benchmark, file_required=False)

    return parser


def main(argv: list[str] | None = None) -> int:
    if load_dotenv:
        load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command not in {"check", "benchmark"}:
        parser.print_help()
        return 1

    result = convert_markdown_to_tla(_request_from_args(args))
    if args.command == "benchmark":
        sys.stdout.write(_render_benchmark_result(result) + "\n")
    else:
        sys.stdout.write(render_result(result, args.format) + "\n")
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
