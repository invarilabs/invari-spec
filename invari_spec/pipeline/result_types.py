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
    explicit_fairness: list[str] = field(default_factory=list)
    inferred_fairness: list[str] = field(default_factory=list)
    fix_comments: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PipelineTiming:
    stage: str
    seconds: float
    detail: str | None = None


@dataclass(frozen=True)
class ReviewFinding:
    id: str
    kind: Literal["fidelity", "assumption", "suggestion"]
    severity: Literal["blocker", "question", "suggestion"]
    lens: Literal[
        "outcome_correctness",
        "construct_appropriateness",
        "invariant_scoping",
        "state_exhaustiveness",
        "terminal_completeness",
        "liveness_fairness",
        "entity_batch_scope",
    ]
    evidence: str
    required_change: str
    choices: list[str] = field(default_factory=list)
    selected_choice: str | None = None


@dataclass(frozen=True)
class AssumptionDecision:
    id: str
    finding_id: str
    source_attempt: int
    lens: str
    evidence: str
    choices: list[str]
    selected_choice: str
    status: Literal["selected", "accepted", "contradicted_by_source", "needs_new_variant"] = "selected"


@dataclass(frozen=True)
class VariantResult:
    id: str
    assumption_decisions: list[AssumptionDecision]
    status: Literal["pass", "fail", "error"]
    dsl_path: str | None = None
    tla_path: str | None = None
    cfg_path: str | None = None
    tlc_output_path: str | None = None
    validation_error: str | None = None
    tlc_exit_code: int | None = None


@dataclass(frozen=True)
class ReviewSummary:
    outcome: Literal[
        "not_run",
        "no_gaps_found",
        "blockers_found",
        "questions_or_suggestions_only",
        "review_parse_failed",
        "capped",
        "repeated",
        "drifted",
        "validation_failed",
    ]
    review_rounds: int = 0
    repair_rounds: int = 0
    blocker_ids: list[str] = field(default_factory=list)
    assumption_count: int = 0
    assumption_decisions: list[AssumptionDecision] = field(default_factory=list)
    assumption_ledger_path: str | None = None
    repairs_avoided_by_ledger: int = 0
    variant_count: int = 0
    selected_variant_id: str | None = None
    variant_report_path: str | None = None
    variants: list[VariantResult] = field(default_factory=list)


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
    assumption_mode: Literal["off", "default", "explore"] = "default"
    explore_variant_limit: int = 4


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
    explicit_fairness: list[str] = field(default_factory=list)
    inferred_fairness: list[str] = field(default_factory=list)
    tlc_exit_code: int | None = None
    trace: str = ""
    timings: list[PipelineTiming] = field(default_factory=list)
    review_summary: ReviewSummary | None = None

    def to_dict(self) -> dict:
        payload = asdict(self)
        if not self.timings:
            payload.pop("timings", None)
        if self.review_summary is None:
            payload.pop("review_summary", None)
        return payload
