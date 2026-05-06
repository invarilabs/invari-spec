from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Literal, Protocol

try:
    from anthropic import Anthropic
except Exception:  # noqa: BLE001
    Anthropic = None  # type: ignore[assignment]

try:
    from openai import OpenAI
except Exception:  # noqa: BLE001
    OpenAI = None  # type: ignore[assignment]

from invari_spec.pipeline.result_types import DslGenerationAttempt, Fixture, MarkdownToTlaRequest, MarkdownToTlaResult
from invari_spec.semantic_dsl import build_cfg, lower_to_tla, parse_dsl_source
from invari_spec.semantic_dsl.errors import DslError
from invari_spec.semantic_dsl.model import WorkflowModel


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT_PATH = ROOT / "invari_spec" / "prompts" / "markdown_to_semantic_dsl_prompt_v1.txt"
DEFAULT_FIXTURE_ROOT = ROOT / "examples"
DEFAULT_FIXTURE_ORDER = (
    "workflow_retry_with_fallback",
    "missing_fallback",
    "infinite_retry",
    "unreachable_success",
)
MAX_REVIEW_ATTEMPTS = 10
FIX_COMMENT_RE = re.compile(r"^\s*#\s*FIX attempt \d+:\s*.+$")

DSL_CANONICAL_FORMS = """
Canonical DSL forms:
- workflow("workflow_name")
- entity("entity_name", Record(field_name=Type, ...))
- var("var_name", Type)
- init(predicate1, predicate2, ...)
- action("action_name", requires=[predicate, ...], changes=[update, ...], emits=[expr, ...], ensures=[predicate, ...])
- invariant("name", predicate)
- forbidden("name", when=predicate)
- obligation("name", trigger=predicate, must_eventually=predicate)
- completion_requires("name"?, outcome=predicate, condition=predicate)

Canonical predicate and reference forms:
- Var("name")
- Field("entity", "field")
- Eq(a, b)
- And(predicate1, predicate2, ...)
- Or(predicate1, predicate2, ...)
- Not(predicate)
- Implies(condition, consequence)
- Add(a, b)
- Sub(a, b)
- Lt(a, b)
- Le(a, b)
- Gt(a, b)
- Ge(a, b)
- Contains(collection, item)
- Count(collection)
- CountUnique(collection)
- Changed(Var("name")) or Changed(Field("entity", "field"))
- Unchanged(Var("name")) or Unchanged(Field("entity", "field"))

Canonical update forms:
- Set("var_name", value)
- SetField("entity", "field", value)
""".strip()


def _build_validation_repair_hints(validation_error: str) -> list[str]:
    error = validation_error.strip().lower()
    hints = [
        "Repair every instance of the same malformed DSL pattern across the file, even if the validator reported only the first one.",
    ]
    if "forbidden(" in error:
        hints.extend(
            [
                'Expected form: forbidden("name", when=<predicate>)',
                'Suggested fix: if you see forbidden("name", <expr>), rewrite it as forbidden("name", when=<expr>).',
            ]
        )
    if "obligation(" in error:
        hints.append('Expected form: obligation("name", trigger=<predicate>, must_eventually=<predicate>)')
    if "completion_requires(" in error:
        hints.append('Expected form: completion_requires("name"?, outcome=<predicate>, condition=<predicate>)')
    if "set(" in error or "setfield(" in error:
        hints.extend(
            [
                'Expected Set form: Set("var_name", value)',
                'Expected SetField form: SetField("entity", "field", value)',
            ]
        )
    if "eq(" in error or "not(" in error:
        hints.extend(
            [
                'Expected predicate refs: Var("name") or Field("entity", "field")',
                'Suggested fix: rewrite Eq("x", value) to Eq(Var("x"), value).',
                'Suggested fix: rewrite Not("x") to Not(Var("x")).',
            ]
        )
    if "missing init" in error:
        hints.append("Suggested fix: add the missing init(...) predicates for every declared field or variable listed in the error.")
    if "missing # fix attempt" in error:
        hints.append("Suggested fix: add a '# FIX attempt <n>: ...' comment immediately above each changed line or block.")
    return hints


class LLMClient(Protocol):
    def generate(self, prompt: str, model: str | None = None, max_tokens: int = 16384) -> str:
        ...


class DefaultSpecDebuggingLLMClient:
    # This initializer accepts standard provider environment variables and keeps legacy names as fallback for smoother extraction.
    def __init__(self, provider: str | None = None) -> None:
        self.provider = (provider or "").strip().lower() or None
        openai_key = os.getenv("OPENAI_API_KEY", "").strip() or os.getenv("INVARI_OPENAI_API_KEY", "").strip()
        claude_key = os.getenv("ANTHROPIC_API_KEY", "").strip() or os.getenv("INVARI_CLAUDE_API_KEY", "").strip()
        self.openai_client = OpenAI(api_key=openai_key) if openai_key and OpenAI else None
        self.claude_client = Anthropic(api_key=claude_key) if claude_key and Anthropic else None

    def _resolve_provider(self) -> str | None:
        if self.provider in {"openai", "claude"}:
            return self.provider
        if self.openai_client:
            return "openai"
        if self.claude_client:
            return "claude"
        return None

    def generate(self, prompt: str, model: str | None = None, max_tokens: int = 16384) -> str:
        provider = self._resolve_provider()
        if not provider:
            return ""
        if provider == "openai" and self.openai_client:
            model_name = model or "gpt-4o-mini"
            resp = self.openai_client.chat.completions.create(
                model=model_name,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": prompt}],
            )
            return resp.choices[0].message.content or ""
        if provider == "claude" and self.claude_client:
            model_name = model or "claude-haiku-4-5-20251001"
            resp = self.claude_client.messages.create(
                model=model_name,
                max_tokens=min(max_tokens, 4096),
                messages=[{"role": "user", "content": prompt}],
            )
            chunks = [block.text for block in resp.content if getattr(block, "type", "") == "text"]
            return "\n".join(chunks).strip()
        return ""


def _slugify(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("._-")
    return slug or "spec"


def _operator_name(value: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", value)
    candidate = "".join(part[:1].upper() + part[1:] for part in parts if part)
    if not candidate:
        return "SpecModel"
    if candidate[0].isdigit():
        return f"M_{candidate}"
    return candidate


def _resolve_generated_root(path: Path, cwd: Path) -> Path:
    expanded = path.expanduser()
    if expanded.is_absolute():
        return expanded
    return (cwd / expanded).resolve()


def _default_tla_jar_candidates() -> list[str]:
    return [
        str(ROOT / "third_party" / "tla2tools.jar"),
    ]


def _find_tla_jar(explicit_path: Path | None = None) -> Path:
    candidates: list[str] = []
    if explicit_path is not None:
        candidates.append(str(explicit_path.expanduser().resolve()))
    candidates.extend(candidate for candidate in _default_tla_jar_candidates() if candidate)
    for candidate in candidates:
        path = Path(candidate)
        if path.exists():
            return path
    tried = "\n".join(f"- {candidate}" for candidate in candidates) if candidates else "- (none)"
    raise FileNotFoundError(
        "Unable to locate tla2tools.jar. Run scripts/setup.sh or pass --tla-jar-path.\nTried:\n"
        f"{tried}"
    )


def _extract_tlc_trace(raw: str) -> str:
    marker = "Error: The behavior up to this point is:"
    idx = raw.find(marker)
    if idx == -1:
        return ""
    return raw[idx:].strip()


def _run_tlc(tla_path: Path, cfg_path: Path, tla_jar_path: Path | None = None) -> tuple[Literal["pass", "fail"], int, str, str]:
    jar = _find_tla_jar(tla_jar_path)
    cmd = ["java", "-cp", str(jar), "tlc2.TLC", "-config", cfg_path.name, tla_path.name]
    proc = subprocess.run(
        cmd,
        cwd=str(tla_path.parent),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    raw = proc.stdout
    has_error = "Error:" in raw or "*** Errors:" in raw or "***Parse Error***" in raw
    status: Literal["pass", "fail"] = "pass" if proc.returncode == 0 and not has_error else "fail"
    return status, proc.returncode, raw, _extract_tlc_trace(raw)


def _copy_input(input_path: Path, run_dir: Path) -> None:
    target = run_dir / "input.md"
    if input_path.resolve() != target.resolve():
        shutil.copyfile(input_path, target)


def load_fixtures(fixture_root: Path = DEFAULT_FIXTURE_ROOT) -> list[Fixture]:
    fixtures: list[Fixture] = []
    for name in DEFAULT_FIXTURE_ORDER:
        case_dir = fixture_root / name
        md_path = case_dir / "SPEC.md"
        dsl_path = case_dir / "expected.dsl.py"
        if md_path.exists() and dsl_path.exists():
            fixtures.append(
                Fixture(
                    name=name,
                    markdown=md_path.read_text(encoding="utf-8"),
                    dsl=dsl_path.read_text(encoding="utf-8"),
                )
            )
    return fixtures


def build_initial_markdown_to_dsl_prompt(markdown: str, fixtures: list[Fixture], prompt_path: Path | None = None) -> str:
    base_prompt_path = prompt_path or DEFAULT_PROMPT_PATH
    prompt_text = base_prompt_path.read_text(encoding="utf-8") if base_prompt_path.exists() else ""
    fixture_blocks: list[str] = []
    for fixture in fixtures:
        fixture_blocks.append(
            "\n".join(
                [
                    f"### Example: {fixture.name}",
                    "Input spec markdown:",
                    "```markdown",
                    fixture.markdown.strip(),
                    "```",
                    "Expected semantic DSL:",
                    "```python",
                    fixture.dsl.strip(),
                    "```",
                ]
            )
        )
    return "\n\n".join(
        [
            prompt_text.strip(),
            "## Few-shot examples",
            "\n\n".join(fixture_blocks).strip(),
            "## Target spec markdown",
            "```markdown",
            markdown.strip(),
            "```",
            "Return only the semantic DSL source for the target spec markdown.",
        ]
    ).strip() + "\n"


def build_dsl_fidelity_review_prompt(*, markdown: str, dsl_source: str) -> str:
    return (
        "\n\n".join(
            [
                "You are a formal modeling reviewer checking whether a generated semantic DSL file faithfully captures a prose workflow spec.",
                "Apply these lenses in order: (1) outcome correctness, (2) construct appropriateness, (3) invariant scoping, (4) state exhaustiveness, (5) terminal completeness, (6) entity/batch scope.",
                "If there are no substantive modeling gaps, return exactly: No gaps found.",
                "If there are gaps, for each gap found, output: Why it matters / What to update / What DSL action to take.",
                "Format each as a numbered change with sub-bullets.",
                "If any questions remain, list them at the very end under a heading exactly named: Questions:",
                "Under Questions:, put one question per line in the form: question#N: <text>",
                "If no questions remain, omit the Questions: section entirely.",
                "Original spec markdown:",
                "```markdown",
                markdown.strip(),
                "```",
                "Generated semantic DSL:",
                "```python",
                dsl_source.strip(),
                "```",
            ]
        ).strip()
        + "\n"
    )


def build_dsl_review_repair_prompt(*, markdown: str, previous_output: str, review_feedback: str) -> str:
    return (
        "\n\n".join(
            [
                "You are repairing a semantic DSL file based on formal modeling review feedback.",
                "Apply only the changes needed to address the review feedback while preserving unrelated structure.",
                "First, apply the numbered review changes as-is to the DSL.",
                "Next, answer every question in the trailing Questions: section conservatively using the markdown and current DSL.",
                "If a question cannot be deduced, choose the smallest assumption that preserves spec intent.",
                "Apply any DSL changes implied by those answers as well.",
                "Return exactly two fenced blocks and nothing else.",
                "Begin your response with the ```python fenced block.",
                "Do not include any prose before the first fence.",
                "The first fenced block must be ```python and contain only the full corrected DSL file.",
                "If you cannot improve the DSL, still return the previous DSL unchanged inside the ```python block.",
                "The second fenced block must be ```text and contain only answered questions in this exact format:",
                "question#1: ...",
                "answer#1: ...",
                "question#2: ...",
                "answer#2: ...",
                "If there were no questions to answer, return an empty ```text block.",
                "Original spec markdown:",
                "```markdown",
                markdown.strip(),
                "```",
                "Previous DSL output:",
                "```python",
                previous_output.strip(),
                "```",
                "Formal modeling review feedback:",
                review_feedback.strip(),
            ]
        ).strip()
        + "\n"
    )


def build_assumptions_summary_prompt(*, assumptions_text: str) -> str:
    return (
        "\n\n".join(
            [
                "Summarize these modeling assumptions in natural language for a developer reviewing the generated spec artifacts.",
                "Keep it concise and practical.",
                "Explain what was assumed and the likely behavioral impact of each assumption.",
                "Assumptions artifact:",
                "```text",
                assumptions_text.strip(),
                "```",
            ]
        ).strip()
        + "\n"
    )


def build_minimal_dsl_repair_prompt(
    *,
    markdown: str,
    previous_output: str,
    validation_error: str,
    attempt_no: int,
    warnings: list[str] | None = None,
) -> str:
    parts = [
        "You are repairing a semantic DSL file.",
        "Do not rewrite the whole file.",
        "Do not rename unrelated entities, fields, actions, or properties.",
        "Do not reorder unrelated declarations.",
        "Do not change unrelated requirements.",
        "Only modify the failing line or the smallest block required to fix the validation error.",
        "Repair every instance of the same malformed DSL problem across the file, even if the validator only reported the first one.",
        "For var(...) state updates, use Set(\"name\", value), not Set(Var(\"name\"), value).",
        "For var(...) predicate references, use Var(\"name\"), not a bare string literal.",
        f"Add a short comment immediately above each changed line or block: # FIX attempt {attempt_no}: <short reason>",
        "Return the full corrected DSL file after applying the minimal edit.",
        DSL_CANONICAL_FORMS,
        "Validation error:",
        validation_error.strip(),
        "Validator repair hints:",
        "\n".join(f"- {hint}" for hint in _build_validation_repair_hints(validation_error)),
    ]
    if warnings:
        parts.extend(
            [
                "Exploration modeling warnings to keep in mind while fixing the hard error:",
                "\n".join(f"- {warning}" for warning in warnings),
            ]
        )
    parts.extend(
        [
            "Original spec markdown:",
            "```markdown",
            markdown.strip(),
            "```",
            "Previous DSL output to repair:",
            "```python",
            previous_output.strip(),
            "```",
        ]
    )
    return "\n\n".join(parts).strip() + "\n"


def normalize_llm_dsl_output(raw: str) -> str:
    text = raw.strip()
    if not text:
        raise ValueError("LLM returned empty DSL output")

    fenced = re.findall(r"```([A-Za-z0-9_-]*)\n(.*?)```", text, flags=re.DOTALL)
    if fenced:
        preferred = None
        fallback = None
        for lang, body in fenced:
            candidate = body.strip()
            if not candidate:
                continue
            fallback = fallback or candidate
            if lang.strip().lower() in {"", "py", "python", "dsl"}:
                preferred = candidate
                break
        text = preferred or fallback or ""

    if not text.strip():
        raise ValueError("LLM output normalization produced empty DSL source")
    if not re.search(r"(?m)^\s*workflow\s*\(", text):
        raise ValueError("LLM output does not contain a workflow(...) DSL declaration")
    return normalize_common_dsl_syntax(text).rstrip() + "\n"


def _review_has_questions(review_feedback: str) -> bool:
    return bool(re.search(r"(?mi)^\s*Questions:\s*$", review_feedback))


def _parse_review_repair_response(raw: str) -> tuple[str, list[tuple[str, str]]]:
    fenced = re.findall(r"```([A-Za-z0-9_-]*)\n(.*?)```", raw.strip(), flags=re.DOTALL)
    dsl_block = None
    qa_block = None
    for lang, body in fenced:
        normalized_lang = lang.strip().lower()
        if dsl_block is None and normalized_lang in {"python", "py"}:
            dsl_block = body.strip()
            continue
        if qa_block is None and normalized_lang == "text":
            qa_block = body.strip()
    if not dsl_block:
        raise ValueError("review repair response missing python fenced DSL block")

    pairs: list[tuple[str, str]] = []
    if qa_block:
        expected_idx = 1
        pending_question: tuple[int, str] | None = None
        for raw_line in qa_block.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            question_match = re.match(r"(?i)^question#(\d+):\s*(.+)$", line)
            if question_match:
                idx = int(question_match.group(1))
                if pending_question is not None:
                    raise ValueError("review repair response has question without matching answer")
                if idx != expected_idx:
                    raise ValueError("review repair response has inconsistent question numbering")
                pending_question = (idx, question_match.group(2).strip())
                continue
            answer_match = re.match(r"(?i)^answer#(\d+):\s*(.+)$", line)
            if answer_match:
                idx = int(answer_match.group(1))
                if pending_question is None:
                    raise ValueError("review repair response has answer without matching question")
                if idx != pending_question[0]:
                    raise ValueError("review repair response has mismatched question and answer numbering")
                pairs.append((pending_question[1], answer_match.group(2).strip()))
                pending_question = None
                expected_idx += 1
                continue
            raise ValueError("review repair response has malformed Q/A text block")
        if pending_question is not None:
            raise ValueError("review repair response has question without matching answer")

    return normalize_common_dsl_syntax(dsl_block).rstrip() + "\n", pairs


def normalize_common_dsl_syntax(source: str) -> str:
    source = re.sub(r'Set\(\s*Var\(\s*"([^"]+)"\s*\)\s*,', r'Set("\1",', source)
    source = re.sub(r'Eq\(\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*,', r'Eq(Var("\1"),', source)
    source = re.sub(r'Not\(\s*"([A-Za-z_][A-Za-z0-9_]*)"\s*\)', r'Not(Var("\1"))', source)
    source = re.sub(r'forbidden\(\s*("[^"]+")\s*,(?!\s*when\s*=)\s*', r'forbidden(\1, when=', source)
    source = re.sub(r'(?:when\s*=\s*){2,}', 'when=', source)
    return source


def validate_dsl_source(source: str, source_name: str) -> WorkflowModel:
    return parse_dsl_source(source, source_name=source_name)


def extract_fix_comments(source: str) -> list[str]:
    return [line.strip() for line in source.splitlines() if FIX_COMMENT_RE.match(line)]


def _has_attempt_fix_comment(source: str, attempt_no: int) -> bool:
    prefix = f"# FIX attempt {attempt_no}:"
    return any(line.strip().startswith(prefix) for line in source.splitlines())


def _comparable_validation_error(message: str | None) -> str:
    if not message:
        return ""
    text = message.strip()
    if ": " in text:
        _, remainder = text.split(": ", 1)
        if re.match(r"[A-Za-z_].*", remainder):
            return remainder
    return text


def _review_says_no_gaps(review_feedback: str) -> bool:
    return "no gaps found" in review_feedback.lower()


def write_initial_dsl_artifact(*, run_dir: Path, candidate_source: str) -> Path:
    initial_path = run_dir / "initial.dsl.py"
    initial_path.write_text(candidate_source, encoding="utf-8")
    return initial_path


def write_validation_attempt_artifacts(
    *,
    run_dir: Path,
    attempt_no: int,
    candidate_source: str,
    validation_error: str | None,
) -> tuple[Path, Path | None]:
    attempts_dir = run_dir / "dsl_validation_attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    candidate_path = attempts_dir / f"attempt_{attempt_no}.dsl.py"
    candidate_path.write_text(candidate_source, encoding="utf-8")
    validation_path = None
    if validation_error is not None:
        validation_path = attempts_dir / f"attempt_{attempt_no}.validation.txt"
        validation_path.write_text(validation_error.rstrip() + "\n", encoding="utf-8")
    return candidate_path, validation_path


def write_review_feedback_artifact(*, run_dir: Path, attempt_no: int, review_feedback: str) -> Path:
    attempts_dir = run_dir / "review_attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    review_path = attempts_dir / f"attempt_{attempt_no}.review.txt"
    review_path.write_text(review_feedback.rstrip() + "\n", encoding="utf-8")
    return review_path


def write_review_repair_artifact(*, run_dir: Path, attempt_no: int, candidate_source: str) -> Path:
    attempts_dir = run_dir / "review_attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    repair_path = attempts_dir / f"attempt_{attempt_no}.dsl.py"
    repair_path.write_text(candidate_source, encoding="utf-8")
    return repair_path


def write_review_repair_response_artifact(*, run_dir: Path, attempt_no: int, raw_response: str) -> Path:
    attempts_dir = run_dir / "review_attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    response_path = attempts_dir / f"attempt_{attempt_no}.repair_response.txt"
    response_path.write_text(raw_response, encoding="utf-8")
    return response_path


def append_review_assumptions_artifact(*, run_dir: Path, attempt_no: int, qa_pairs: list[tuple[str, str]]) -> Path:
    attempts_dir = run_dir / "review_attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    assumptions_path = attempts_dir / "assumptions.txt"
    if not qa_pairs:
        raise ValueError("cannot append empty review assumptions entry")
    lines = [f"attempt #{attempt_no}"]
    for idx, (question, answer) in enumerate(qa_pairs, start=1):
        lines.append(f"question#{idx}: {question}")
        lines.append(f"answer#{idx}: {answer}")
    with assumptions_path.open("a", encoding="utf-8") as fh:
        if assumptions_path.stat().st_size > 0:
            fh.write("\n")
        fh.write("\n".join(lines) + "\n")
    return assumptions_path


def write_assumptions_summary_artifact(*, run_dir: Path, summary_text: str) -> Path:
    attempts_dir = run_dir / "review_attempts"
    attempts_dir.mkdir(parents=True, exist_ok=True)
    summary_path = attempts_dir / "assumptions_summary.txt"
    summary_path.write_text(summary_text.rstrip() + "\n", encoding="utf-8")
    return summary_path


def write_tla_artifacts(model: WorkflowModel, run_dir: Path) -> tuple[Path, Path]:
    module_name = _operator_name(model.name)
    tla_path = run_dir / f"{module_name}.tla"
    cfg_path = run_dir / f"{module_name}.cfg"
    tla_path.write_text(lower_to_tla(model, module_name=module_name), encoding="utf-8")
    cfg_path.write_text(build_cfg(model), encoding="utf-8")
    return tla_path, cfg_path


def _underspecified_assumptions(warnings: list[str]) -> list[str]:
    assumptions: list[str] = []
    for warning in warnings:
        if ": " in warning:
            prefix, detail = warning.split(": ", 1)
            if prefix.startswith("W_EXPLORATION_"):
                assumptions.append(detail)
    return assumptions


def _classify_bug_classes(status: str, warnings: list[str], trace: str) -> list[str]:
    classes: list[str] = []
    joined = "\n".join(warnings)
    if "READ_BEFORE_CREATE" in joined:
        classes.append("missing-fallback")
    if "FROZEN_OUTCOME" in joined or "DEAD_BRANCH" in joined:
        classes.append("underspecification")
    if status == "fail" and trace:
        classes.append("counterexample")
    return classes


def _latest_assumptions_path(attempts: list[DslGenerationAttempt]) -> Path | None:
    for attempt in reversed(attempts):
        if attempt.assumptions_path:
            return Path(attempt.assumptions_path)
    return None


def _build_result(
    *,
    status: Literal["pass", "fail", "error"],
    input_path: Path,
    run_dir: Path,
    dsl_path: Path | None,
    tla_path: Path | None,
    cfg_path: Path | None,
    attempts: list[DslGenerationAttempt],
    validation_error: str | None,
    tlc_output_path: Path | None,
    fix_comments: list[str],
    warnings: list[str],
    summary: str,
    phase: str,
    tlc_exit_code: int | None,
    trace: str,
    has_liveness: bool,
    extra_notes: list[str] | None = None,
) -> MarkdownToTlaResult:
    underspecified = _underspecified_assumptions(warnings)
    fairness_sensitive = status == "fail" and has_liveness
    if not has_liveness:
        liveness_classification: Literal["confirmed_failure", "missing_fairness", "not_applicable"] = "not_applicable"
    elif fairness_sensitive:
        liveness_classification = "missing_fairness"
    elif status == "fail":
        liveness_classification = "confirmed_failure"
    else:
        liveness_classification = "not_applicable"
    notes: list[str] = []
    if fairness_sensitive:
        notes.append("Liveness failed under a spec without explicit fairness assumptions; interpret the trace as fairness-sensitive.")
    if underspecified:
        notes.append("Some behavior remains underspecified and is surfaced separately from the TLC result.")
    notes.extend(extra_notes or [])
    return MarkdownToTlaResult(
        status=status,
        input_path=str(input_path),
        run_dir=str(run_dir),
        dsl_path=str(dsl_path) if dsl_path else None,
        tla_path=str(tla_path) if tla_path else None,
        cfg_path=str(cfg_path) if cfg_path else None,
        attempts=attempts,
        validation_error=validation_error,
        tlc_output_path=str(tlc_output_path) if tlc_output_path else None,
        attempt_count=len(attempts),
        fix_comments=fix_comments,
        warnings=warnings,
        summary=summary,
        phase=phase,
        bug_classes=_classify_bug_classes(status, warnings, trace),
        underspecified_assumptions=underspecified,
        fairness_sensitive=fairness_sensitive,
        liveness_classification=liveness_classification,
        notes=notes,
        tlc_exit_code=tlc_exit_code,
        trace=trace,
    )


def _write_result(result: MarkdownToTlaResult) -> None:
    result_path = Path(result.run_dir) / "result.json"
    result_path.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")


def _error_result(
    *,
    req: MarkdownToTlaRequest,
    input_path: Path,
    run_dir: Path,
    attempts: list[DslGenerationAttempt],
    phase: str,
    summary: str,
    validation_error: str | None = None,
    dsl_path: Path | None = None,
    tla_path: Path | None = None,
    cfg_path: Path | None = None,
    tlc_output_path: Path | None = None,
    tlc_exit_code: int | None = None,
    trace: str = "",
    warnings: list[str] | None = None,
    has_liveness: bool = False,
) -> MarkdownToTlaResult:
    _ = req
    fix_comments: list[str] = []
    for attempt in attempts:
        fix_comments.extend(attempt.fix_comments)
    result = _build_result(
        status="error",
        input_path=input_path,
        run_dir=run_dir,
        dsl_path=dsl_path,
        tla_path=tla_path,
        cfg_path=cfg_path,
        attempts=attempts,
        validation_error=validation_error,
        tlc_output_path=tlc_output_path,
        fix_comments=fix_comments,
        warnings=list(warnings or []),
        summary=summary,
        phase=phase,
        tlc_exit_code=tlc_exit_code,
        trace=trace,
        has_liveness=has_liveness,
        extra_notes=None,
    )
    _write_result(result)
    return result


def _finalize_validated_model(
    *,
    req: MarkdownToTlaRequest,
    input_path: Path,
    run_dir: Path,
    attempts: list[DslGenerationAttempt],
    model: WorkflowModel,
    final_candidate_path: Path,
    llm_client: LLMClient | None = None,
) -> MarkdownToTlaResult:
    final_dsl_path = run_dir / "final.dsl.py"
    shutil.copyfile(final_candidate_path, final_dsl_path)
    has_liveness = bool(model.obligations)

    try:
        tla_path, cfg_path = write_tla_artifacts(model, run_dir)
    except Exception as exc:  # noqa: BLE001
        return _error_result(
            req=req,
            input_path=input_path,
            run_dir=run_dir,
            attempts=attempts,
            phase="tla_lowering",
            summary=f"TLA+ lowering failed: {exc}",
            validation_error=str(exc),
            dsl_path=final_dsl_path,
            has_liveness=has_liveness,
        )

    tlc_output_path = None
    tlc_exit_code = None
    trace = ""
    status: Literal["pass", "fail", "error"] = "pass"
    phase = "complete"
    summary = "Generated validated DSL, TLA+, and CFG"
    warnings = list(model.warnings)
    extra_notes: list[str] = []
    assumptions_summary_path: Path | None = None
    assumptions_summary_error: str | None = None
    summary_thread: threading.Thread | None = None

    if req.run_tlc:
        assumptions_path = _latest_assumptions_path(attempts)
        if assumptions_path and assumptions_path.exists():
            def _run_assumption_summary() -> None:
                nonlocal assumptions_summary_path, assumptions_summary_error
                try:
                    assumptions_text = assumptions_path.read_text(encoding="utf-8")
                    summary_client = llm_client or DefaultSpecDebuggingLLMClient()
                    summary_text = (summary_client.generate(
                        build_assumptions_summary_prompt(assumptions_text=assumptions_text),
                        model=req.llm_model,
                        max_tokens=2048,
                    )).strip()
                    if not summary_text:
                        raise ValueError("LLM returned empty assumptions summary")
                    assumptions_summary_path = write_assumptions_summary_artifact(
                        run_dir=run_dir,
                        summary_text=summary_text,
                    )
                except Exception as exc:  # noqa: BLE001
                    assumptions_summary_error = str(exc)

            summary_thread = threading.Thread(target=_run_assumption_summary, daemon=True)
            summary_thread.start()
        tlc_output_path = run_dir / "tlc.out"
        try:
            tlc_status, tlc_exit_code, tlc_raw, trace = _run_tlc(tla_path, cfg_path, req.tla_jar_path)
            tlc_output_path.write_text(tlc_raw, encoding="utf-8")
            status = tlc_status
            summary = "TLC passed" if tlc_status == "pass" else "TLC reported a failure"
        except Exception as exc:  # noqa: BLE001
            if summary_thread is not None:
                summary_thread.join()
            tlc_output_path.write_text(str(exc).rstrip() + "\n", encoding="utf-8")
            if assumptions_summary_path is not None:
                extra_notes.append(f"Assumptions summary: {assumptions_summary_path}")
            elif assumptions_summary_error:
                extra_notes.append(f"Assumptions summary failed: {assumptions_summary_error}")
            return _error_result(
                req=req,
                input_path=input_path,
                run_dir=run_dir,
                attempts=attempts,
                phase="tlc",
                summary=f"TLC setup or execution failed: {exc}",
                validation_error=str(exc),
                dsl_path=final_dsl_path,
                tla_path=tla_path,
                cfg_path=cfg_path,
                tlc_output_path=tlc_output_path,
                warnings=warnings,
                has_liveness=has_liveness,
            )
        if summary_thread is not None:
            summary_thread.join()
        if assumptions_summary_path is not None:
            extra_notes.append(f"Assumptions summary: {assumptions_summary_path}")
        elif assumptions_summary_error:
            extra_notes.append(f"Assumptions summary failed: {assumptions_summary_error}")

    all_fix_comments: list[str] = []
    for attempt in attempts:
        all_fix_comments.extend(attempt.fix_comments)
    result = _build_result(
        status=status,
        input_path=input_path,
        run_dir=run_dir,
        dsl_path=final_dsl_path,
        tla_path=tla_path,
        cfg_path=cfg_path,
        attempts=attempts,
        validation_error=None,
        tlc_output_path=tlc_output_path,
        fix_comments=all_fix_comments,
        warnings=warnings,
        summary=summary,
        phase=phase,
        tlc_exit_code=tlc_exit_code,
        trace=trace,
        has_liveness=has_liveness,
        extra_notes=extra_notes,
    )
    _write_result(result)
    return result


def _convert_existing_dsl(
    *,
    req: MarkdownToTlaRequest,
    input_path: Path,
    run_dir: Path,
    dsl_file: Path,
    llm_client: LLMClient | None = None,
) -> MarkdownToTlaResult:
    if not dsl_file.exists() or not dsl_file.is_file():
        raise FileNotFoundError(f"DSL file not found: {dsl_file}")

    candidate_source = normalize_common_dsl_syntax(dsl_file.read_text(encoding="utf-8"))
    initial_path = write_initial_dsl_artifact(run_dir=run_dir, candidate_source=candidate_source)
    candidate_path, validation_path = write_validation_attempt_artifacts(
        run_dir=run_dir,
        attempt_no=1,
        candidate_source=candidate_source,
        validation_error=None,
    )
    fix_comments = extract_fix_comments(candidate_source)
    attempts: list[DslGenerationAttempt] = []

    try:
        model = validate_dsl_source(candidate_source, str(candidate_path))
    except DslError as exc:
        validation_error = str(exc)
        validation_path = run_dir / "dsl_validation_attempts" / "attempt_1.validation.txt"
        validation_path.write_text(validation_error.rstrip() + "\n", encoding="utf-8")
        attempts.append(
            DslGenerationAttempt(
                attempt=1,
                status="invalid",
                candidate_path=str(candidate_path),
                validation_error_path=str(validation_path),
                validation_error=validation_error,
                fix_comments=fix_comments,
            )
        )
        return _error_result(
            req=req,
            input_path=input_path,
            run_dir=run_dir,
            attempts=attempts,
            phase="dsl_validation",
            summary="Existing DSL file failed validation",
            validation_error=validation_error,
            dsl_path=initial_path,
        )

    attempts.append(
        DslGenerationAttempt(
            attempt=1,
            status="valid",
            candidate_path=str(candidate_path),
            validation_error_path=str(validation_path) if validation_path else None,
            validation_error=None,
            fix_comments=fix_comments,
        )
    )
    return _finalize_validated_model(
        req=req,
        input_path=input_path,
        run_dir=run_dir,
        attempts=attempts,
        model=model,
        final_candidate_path=candidate_path,
        llm_client=llm_client,
    )


def convert_markdown_to_tla(req: MarkdownToTlaRequest, *, llm_client: LLMClient | None = None) -> MarkdownToTlaResult:
    input_path = req.input_path.expanduser().resolve()
    if not input_path.exists() or not input_path.is_file():
        raise FileNotFoundError(f"spec markdown not found: {input_path}")
    if req.max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    generated_root = _resolve_generated_root(req.generated_root, req.cwd)
    run_dir = generated_root / "invari_spec_check" / _slugify(input_path.stem)
    run_dir.mkdir(parents=True, exist_ok=True)
    _copy_input(input_path, run_dir)

    if req.dsl_file is not None:
        return _convert_existing_dsl(
            req=req,
            input_path=input_path,
            run_dir=run_dir,
            dsl_file=req.dsl_file.expanduser().resolve(),
            llm_client=llm_client,
        )

    markdown = input_path.read_text(encoding="utf-8")
    fixtures = load_fixtures()
    client = llm_client or DefaultSpecDebuggingLLMClient()

    attempts: list[DslGenerationAttempt] = []
    previous_output = ""
    validation_error = ""
    warning_hints: list[str] = []
    model: WorkflowModel | None = None
    final_candidate_path: Path | None = None
    initial_dsl_path: Path | None = None

    for attempt_no in range(1, req.max_attempts + 1):
        review_feedback_path: Path | None = None
        review_repair_path: Path | None = None
        assumptions_path: Path | None = None
        if attempt_no == 1:
            prompt = build_initial_markdown_to_dsl_prompt(markdown, fixtures, req.prompt_path)
        else:
            if len(attempts) >= 2:
                prev = _comparable_validation_error(attempts[-1].validation_error)
                prev_prev = _comparable_validation_error(attempts[-2].validation_error)
                if prev and prev == prev_prev:
                    validation_error = attempts[-1].validation_error or prev
                    break
            prompt = build_minimal_dsl_repair_prompt(
                markdown=markdown,
                previous_output=previous_output,
                validation_error=validation_error,
                attempt_no=attempt_no,
                warnings=warning_hints,
            )

        try:
            candidate_source = normalize_llm_dsl_output(client.generate(prompt, model=req.llm_model, max_tokens=16384))
        except Exception as exc:  # noqa: BLE001
            candidate_source = ""
            validation_error = str(exc)
            candidate_path, validation_path = write_validation_attempt_artifacts(
                run_dir=run_dir,
                attempt_no=attempt_no,
                candidate_source=candidate_source,
                validation_error=validation_error,
            )
            attempts.append(
                DslGenerationAttempt(
                    attempt=attempt_no,
                    status="empty",
                    candidate_path=str(candidate_path),
                    validation_error_path=str(validation_path) if validation_path else None,
                    validation_error=validation_error,
                    review_feedback_path=str(review_feedback_path) if review_feedback_path else None,
                    review_repair_path=str(review_repair_path) if review_repair_path else None,
                    assumptions_path=str(assumptions_path) if assumptions_path else None,
                    fix_comments=[],
                )
            )
            warning_hints = []
            continue

        if attempt_no == 1:
            initial_dsl_path = write_initial_dsl_artifact(run_dir=run_dir, candidate_source=candidate_source)
            for review_attempt_no in range(1, MAX_REVIEW_ATTEMPTS + 1):
                review_prompt = build_dsl_fidelity_review_prompt(markdown=markdown, dsl_source=candidate_source)
                review_feedback = client.generate(review_prompt, model=req.llm_model, max_tokens=4096).strip()
                review_feedback_path = write_review_feedback_artifact(
                    run_dir=run_dir,
                    attempt_no=review_attempt_no,
                    review_feedback=review_feedback or "No review feedback returned.",
                )
                if not review_feedback or _review_says_no_gaps(review_feedback):
                    break
                review_repair_prompt = build_dsl_review_repair_prompt(
                    markdown=markdown,
                    previous_output=candidate_source,
                    review_feedback=review_feedback,
                )
                raw_repair_response = client.generate(review_repair_prompt, model=req.llm_model, max_tokens=16384)
                write_review_repair_response_artifact(
                    run_dir=run_dir,
                    attempt_no=review_attempt_no,
                    raw_response=raw_repair_response,
                )
                try:
                    candidate_source, qa_pairs = _parse_review_repair_response(raw_repair_response)
                except Exception as exc:  # noqa: BLE001
                    validation_error = str(exc)
                    candidate_path, validation_path = write_validation_attempt_artifacts(
                        run_dir=run_dir,
                        attempt_no=attempt_no,
                        candidate_source="",
                        validation_error=validation_error,
                    )
                    attempts.append(
                        DslGenerationAttempt(
                            attempt=attempt_no,
                            status="empty",
                            candidate_path=str(candidate_path),
                            validation_error_path=str(validation_path) if validation_path else None,
                            validation_error=validation_error,
                            review_feedback_path=str(review_feedback_path) if review_feedback_path else None,
                            review_repair_path=str(review_repair_path) if review_repair_path else None,
                            assumptions_path=str(assumptions_path) if assumptions_path else None,
                            fix_comments=[],
                        )
                    )
                    return _error_result(
                        req=req,
                        input_path=input_path,
                        run_dir=run_dir,
                        attempts=attempts,
                        phase="dsl_generation",
                        summary=f"Review repair failed: {exc}",
                        validation_error=validation_error,
                        dsl_path=initial_dsl_path,
                    )
                if _review_has_questions(review_feedback) and not qa_pairs:
                    validation_error = "review repair response missing question/answer pairs for review questions"
                    candidate_path, validation_path = write_validation_attempt_artifacts(
                        run_dir=run_dir,
                        attempt_no=attempt_no,
                        candidate_source="",
                        validation_error=validation_error,
                    )
                    attempts.append(
                        DslGenerationAttempt(
                            attempt=attempt_no,
                            status="empty",
                            candidate_path=str(candidate_path),
                            validation_error_path=str(validation_path) if validation_path else None,
                            validation_error=validation_error,
                            review_feedback_path=str(review_feedback_path) if review_feedback_path else None,
                            review_repair_path=str(review_repair_path) if review_repair_path else None,
                            assumptions_path=str(assumptions_path) if assumptions_path else None,
                            fix_comments=[],
                        )
                    )
                    return _error_result(
                        req=req,
                        input_path=input_path,
                        run_dir=run_dir,
                        attempts=attempts,
                        phase="dsl_generation",
                        summary="Review repair failed: missing question answers",
                        validation_error=validation_error,
                        dsl_path=initial_dsl_path,
                    )
                review_repair_path = write_review_repair_artifact(
                    run_dir=run_dir,
                    attempt_no=review_attempt_no,
                    candidate_source=candidate_source,
                )
                if qa_pairs:
                    assumptions_path = append_review_assumptions_artifact(
                        run_dir=run_dir,
                        attempt_no=review_attempt_no,
                        qa_pairs=qa_pairs,
                    )
                previous_output = candidate_source

        candidate_path, _ = write_validation_attempt_artifacts(
            run_dir=run_dir,
            attempt_no=attempt_no,
            candidate_source=candidate_source,
            validation_error=None,
        )
        fix_comments = extract_fix_comments(candidate_source)

        try:
            if attempt_no > 1 and not _has_attempt_fix_comment(candidate_source, attempt_no):
                raise ValueError(f"repair attempt {attempt_no} missing # FIX attempt {attempt_no}: comment")
            model = validate_dsl_source(candidate_source, str(candidate_path))
        except (DslError, ValueError) as exc:
            validation_error = str(exc)
            validation_path = run_dir / "dsl_validation_attempts" / f"attempt_{attempt_no}.validation.txt"
            validation_path.write_text(validation_error.rstrip() + "\n", encoding="utf-8")
            attempts.append(
                DslGenerationAttempt(
                    attempt=attempt_no,
                    status="invalid",
                    candidate_path=str(candidate_path),
                    validation_error_path=str(validation_path),
                    validation_error=validation_error,
                    review_feedback_path=str(review_feedback_path) if review_feedback_path else None,
                    review_repair_path=str(review_repair_path) if review_repair_path else None,
                    assumptions_path=str(assumptions_path) if assumptions_path else None,
                    fix_comments=fix_comments,
                )
            )
            previous_output = candidate_source
            warning_hints = []
            continue

        attempts.append(
            DslGenerationAttempt(
                attempt=attempt_no,
                status="valid",
                candidate_path=str(candidate_path),
                validation_error_path=None,
                validation_error=None,
                review_feedback_path=str(review_feedback_path) if review_feedback_path else None,
                review_repair_path=str(review_repair_path) if review_repair_path else None,
                assumptions_path=str(assumptions_path) if assumptions_path else None,
                fix_comments=fix_comments,
            )
        )
        previous_output = candidate_source
        warning_hints = list(model.warnings)
        final_candidate_path = candidate_path
        break

    if model is None or final_candidate_path is None:
        return _error_result(
            req=req,
            input_path=input_path,
            run_dir=run_dir,
            attempts=attempts,
            phase="dsl_validation",
            summary="DSL generation failed validation",
            validation_error=validation_error or None,
            dsl_path=initial_dsl_path,
            warnings=warning_hints,
        )

    return _finalize_validated_model(
        req=req,
        input_path=input_path,
        run_dir=run_dir,
        attempts=attempts,
        model=model,
        final_candidate_path=final_candidate_path,
        llm_client=client,
    )


def render_result(result: MarkdownToTlaResult | dict, fmt: str) -> str:
    payload = result.to_dict() if isinstance(result, MarkdownToTlaResult) else result
    if fmt == "json":
        return json.dumps(payload, indent=2)

    lines = [
        f"STATUS: {str(payload.get('status', 'error')).upper()}",
        f"PHASE: {payload.get('phase', '')}",
        f"INPUT: {payload.get('input_path', '')}",
        f"DSL: {payload.get('dsl_path', '')}",
        f"TLA: {payload.get('tla_path', '')}",
        f"CFG: {payload.get('cfg_path', '')}",
        f"ATTEMPTS: {payload.get('attempt_count', 0)}",
        f"BUG_CLASSES: {', '.join(payload.get('bug_classes') or ['(none)'])}",
        f"LIVENESS: {payload.get('liveness_classification', 'not_applicable')}",
        f"FAIRNESS_SENSITIVE: {payload.get('fairness_sensitive', False)}",
        "FIXES:",
    ]
    fixes = payload.get("fix_comments") or []
    if fixes:
        lines.extend(f"- {fix}" for fix in fixes)
    else:
        lines.append("- (none)")
    lines.append("WARNINGS:")
    warnings = payload.get("warnings") or []
    if warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- (none)")
    lines.append("UNDERSPECIFIED:")
    underspecified = payload.get("underspecified_assumptions") or []
    if underspecified:
        lines.extend(f"- {item}" for item in underspecified)
    else:
        lines.append("- (none)")
    notes = payload.get("notes") or []
    if notes:
        lines.append("NOTES:")
        lines.extend(f"- {note}" for note in notes)
    if payload.get("validation_error"):
        lines.append(f"VALIDATION_ERROR: {payload['validation_error']}")
    if payload.get("tlc_exit_code") is not None:
        lines.append(f"TLC_EXIT_CODE: {payload['tlc_exit_code']}")
    if payload.get("tlc_output_path"):
        lines.append(f"TLC_OUTPUT: {payload['tlc_output_path']}")
    if payload.get("trace"):
        lines.extend(["TRACE:", str(payload["trace"])])
    lines.append(f"SUMMARY: {payload.get('summary', '')}")
    return "\n".join(lines)
