from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class Fixture:
    name: str
    markdown: str
    dsl: str


@dataclass(frozen=True)
class DslGenerationAttempt:
    attempt: int
    status: Literal["valid", "invalid", "empty"]
    candidate_path: str | None
    validation_error_path: str | None
    validation_error: str | None
    review_feedback_path: str | None = None
    review_repair_path: str | None = None
    assumptions_path: str | None = None
    fix_comments: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PipelineTiming:
    stage: str
    seconds: float
    detail: str | None = None


@dataclass(frozen=True)
class MarkdownToTlaRequest:
    input_path: Path
    generated_root: Path
    llm_model: str | None = None
    prompt_path: Path | None = None
    dsl_file: Path | None = None
    tla_jar_path: Path | None = None
    max_attempts: int = 3
    run_tlc: bool = True
    cwd: Path = Path.cwd()
    collect_timings: bool = False


@dataclass(frozen=True)
class MarkdownToTlaResult:
    status: Literal["pass", "fail", "error"]
    input_path: str
    run_dir: str
    dsl_path: str | None
    tla_path: str | None
    cfg_path: str | None
    attempts: list[DslGenerationAttempt]
    validation_error: str | None
    tlc_output_path: str | None
    attempt_count: int
    fix_comments: list[str]
    warnings: list[str]
    summary: str
    phase: str
    bug_classes: list[str]
    underspecified_assumptions: list[str]
    fairness_sensitive: bool
    liveness_classification: Literal["confirmed_failure", "missing_fairness", "not_applicable"]
    notes: list[str]
    tlc_exit_code: int | None = None
    trace: str = ""
    timings: list[PipelineTiming] = field(default_factory=list)

    def to_dict(self) -> dict:
        payload = asdict(self)
        if not self.timings:
            payload.pop("timings", None)
        return payload
