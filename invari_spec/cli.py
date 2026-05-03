from __future__ import annotations

import argparse
import sys
from pathlib import Path

from invari_spec.pipeline import MarkdownToTlaRequest, convert_markdown_to_tla, render_result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="invari-spec", description="Debug prose specs with executable checks.")
    subparsers = parser.add_subparsers(dest="command")

    check = subparsers.add_parser("check", help="Run the markdown -> DSL -> TLA+ pipeline on a spec markdown file.")
    check.add_argument("--file", required=True, help="Path to the input spec markdown file.")
    check.add_argument("--dsl-file", help="Resume from an existing DSL attempt file.")
    check.add_argument("--format", choices=("text", "json"), default="text")
    check.add_argument("--output-dir", default="generated", help="Directory for generated artifacts.")
    check.add_argument("--model", help="Optional LLM model name.")
    check.add_argument("--prompt-path", help="Optional replacement prompt file.")
    check.add_argument("--tla-jar-path", help="Path to tla2tools.jar.")
    check.add_argument("--max-attempts", type=int, default=3)
    tlc_group = check.add_mutually_exclusive_group()
    tlc_group.add_argument("--run-tlc", dest="run_tlc", action="store_true", default=True)
    tlc_group.add_argument("--no-run-tlc", dest="run_tlc", action="store_false")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command != "check":
        parser.print_help()
        return 1

    result = convert_markdown_to_tla(
        MarkdownToTlaRequest(
            input_path=Path(args.file),
            generated_root=Path(args.output_dir),
            llm_model=args.model,
            prompt_path=Path(args.prompt_path) if args.prompt_path else None,
            dsl_file=Path(args.dsl_file) if args.dsl_file else None,
            tla_jar_path=Path(args.tla_jar_path) if args.tla_jar_path else None,
            max_attempts=args.max_attempts,
            run_tlc=args.run_tlc,
            cwd=Path.cwd(),
        )
    )
    sys.stdout.write(render_result(result, args.format) + "\n")
    return 0 if result.status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
