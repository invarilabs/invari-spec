from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


LLM_CALL_KINDS = (
    "initial_generation",
    "validation_repair",
    "fidelity_review",
    "fidelity_repair",
    "assumptions_summary",
    "unknown",
)

LIVE_DOC_APPROVAL_BENCHMARK_COMMAND = (
    "set -a; source .env; set +a; "
    "python3 -m invari_spec.cli benchmark test_md/doc_approval.md "
    "--output-dir .invari/tmp/INV-66/doc_approval"
)


@dataclass(frozen=True)
class RecordedLLMCall:
    prompt: str
    classification: str
    model: str | None
    max_tokens: int | None
    response: str


@dataclass(frozen=True)
class BenchmarkResultSummary:
    total_llm_calls: int
    llm_calls_by_kind: dict[str, int]
    fidelity_review_calls: int
    fidelity_repair_calls: int
    validation_repair_calls: int
    stage_totals: dict[str, float]
    stage_counts: dict[str, int]
    final_status: str | None
    final_phase: str | None
    review_classification: str | None
    convergence_classification: str | None
    dsl_line_counts: dict[str, int]

    def to_dict(self) -> dict[str, object]:
        return {
            "total_llm_calls": self.total_llm_calls,
            "llm_calls_by_kind": self.llm_calls_by_kind,
            "fidelity_review_calls": self.fidelity_review_calls,
            "fidelity_repair_calls": self.fidelity_repair_calls,
            "validation_repair_calls": self.validation_repair_calls,
            "stage_totals": self.stage_totals,
            "stage_counts": self.stage_counts,
            "final_status": self.final_status,
            "final_phase": self.final_phase,
            "review_classification": self.review_classification,
            "convergence_classification": self.convergence_classification,
            "dsl_line_counts": self.dsl_line_counts,
        }


@dataclass(frozen=True)
class BenchmarkComparison:
    before: BenchmarkResultSummary
    after: BenchmarkResultSummary
    total_llm_calls_delta: int
    llm_call_deltas_by_kind: dict[str, int]
    stage_total_deltas: dict[str, float]
    stage_count_deltas: dict[str, int]
    dsl_line_count_deltas: dict[str, int]
    status_changed: bool
    phase_changed: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "before": self.before.to_dict(),
            "after": self.after.to_dict(),
            "total_llm_calls_delta": self.total_llm_calls_delta,
            "llm_call_deltas_by_kind": self.llm_call_deltas_by_kind,
            "stage_total_deltas": self.stage_total_deltas,
            "stage_count_deltas": self.stage_count_deltas,
            "dsl_line_count_deltas": self.dsl_line_count_deltas,
            "status_changed": self.status_changed,
            "phase_changed": self.phase_changed,
        }


def compare_benchmark_summaries(
    before: BenchmarkResultSummary,
    after: BenchmarkResultSummary,
) -> BenchmarkComparison:
    return BenchmarkComparison(
        before=before,
        after=after,
        total_llm_calls_delta=after.total_llm_calls - before.total_llm_calls,
        llm_call_deltas_by_kind=_int_deltas(before.llm_calls_by_kind, after.llm_calls_by_kind),
        stage_total_deltas=_float_deltas(before.stage_totals, after.stage_totals),
        stage_count_deltas=_int_deltas(before.stage_counts, after.stage_counts),
        dsl_line_count_deltas=_int_deltas(before.dsl_line_counts, after.dsl_line_counts),
        status_changed=before.final_status != after.final_status,
        phase_changed=before.final_phase != after.final_phase,
    )


def classify_llm_prompt(prompt: str) -> str:
    if "You are repairing a semantic DSL file based on formal modeling review feedback." in prompt:
        return "fidelity_repair"
    if "You are a formal modeling reviewer checking whether a generated semantic DSL file" in prompt:
        return "fidelity_review"
    if "Summarize these modeling assumptions in natural language" in prompt:
        return "assumptions_summary"
    if "You are repairing a semantic DSL file." in prompt and "Validator repair hints:" in prompt:
        return "validation_repair"
    if "## Few-shot examples" in prompt and "## Target spec markdown" in prompt:
        return "initial_generation"
    return "unknown"


class FakeBenchmarkLLMClient:
    def __init__(self, responses: Sequence[str] | Mapping[str, Sequence[str] | str]):
        self.calls: list[RecordedLLMCall] = []
        if isinstance(responses, Mapping):
            self._responses_by_kind = {
                kind: deque([value] if isinstance(value, str) else list(value))
                for kind, value in responses.items()
            }
            self._responses = None
        else:
            self._responses_by_kind = None
            self._responses = deque(responses)

    @property
    def prompts(self) -> list[str]:
        return [call.prompt for call in self.calls]

    def calls_by_kind(self) -> Counter[str]:
        return Counter(call.classification for call in self.calls)

    def generate(self, prompt: str, *, model: str | None = None, max_tokens: int | None = None) -> str:
        classification = classify_llm_prompt(prompt)
        if self._responses_by_kind is not None:
            responses = self._responses_by_kind.get(classification)
            if not responses:
                raise AssertionError(f"no fake LLM response configured for {classification}")
            response = responses.popleft()
        else:
            if not self._responses:
                raise AssertionError(f"no fake LLM response left for {classification}")
            response = self._responses.popleft()
        self.calls.append(
            RecordedLLMCall(
                prompt=prompt,
                classification=classification,
                model=model,
                max_tokens=max_tokens,
                response=response,
            )
        )
        return response


def summarize_benchmark_result(
    result,
    *,
    llm_client: FakeBenchmarkLLMClient | None = None,
    dsl_artifacts: Mapping[str, str | Path | None] | None = None,
) -> BenchmarkResultSummary:
    timings = _get(result, "timings") or []
    stage_totals: dict[str, float] = {}
    stage_counts: Counter[str] = Counter()
    for timing in timings:
        stage = _get(timing, "stage")
        if stage == "total" or not stage:
            continue
        seconds = float(_get(timing, "seconds") or 0.0)
        stage_totals[stage] = stage_totals.get(stage, 0.0) + seconds
        stage_counts[stage] += 1

    calls_by_kind = Counter({kind: 0 for kind in LLM_CALL_KINDS})
    if llm_client is not None:
        calls_by_kind.update(llm_client.calls_by_kind())
    else:
        calls_by_kind.update(_classify_llm_timings(timings))

    final_status = _get(result, "status")
    final_phase = _get(result, "phase")
    return BenchmarkResultSummary(
        total_llm_calls=sum(calls_by_kind.values()),
        llm_calls_by_kind={kind: calls_by_kind[kind] for kind in LLM_CALL_KINDS},
        fidelity_review_calls=calls_by_kind["fidelity_review"],
        fidelity_repair_calls=calls_by_kind["fidelity_repair"],
        validation_repair_calls=calls_by_kind["validation_repair"],
        stage_totals={stage: stage_totals[stage] for stage in sorted(stage_totals)},
        stage_counts={stage: stage_counts[stage] for stage in sorted(stage_counts)},
        final_status=final_status,
        final_phase=final_phase,
        review_classification=_classify_review(result),
        convergence_classification=_classify_convergence(final_status, final_phase),
        dsl_line_counts=_count_dsl_lines(result, dsl_artifacts),
    )


def _get(obj, name: str):
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def _int_deltas(before: Mapping[str, int], after: Mapping[str, int]) -> dict[str, int]:
    return {key: int(after.get(key, 0)) - int(before.get(key, 0)) for key in sorted(set(before) | set(after))}


def _float_deltas(before: Mapping[str, float], after: Mapping[str, float]) -> dict[str, float]:
    return {
        key: float(after.get(key, 0.0)) - float(before.get(key, 0.0))
        for key in sorted(set(before) | set(after))
    }


def _classify_review(result) -> str | None:
    attempts = _get(result, "attempts") or []
    saw_review = False
    saw_repair = False
    saw_no_gaps = False
    for attempt in attempts:
        review_path = _get(attempt, "review_feedback_path")
        repair_path = _get(attempt, "review_repair_path")
        if repair_path:
            saw_repair = True
        if review_path:
            saw_review = True
            path = Path(review_path)
            if path.exists() and "no gaps found" in path.read_text(encoding="utf-8").strip().lower():
                saw_no_gaps = True
    if saw_repair:
        return "review_repaired"
    if saw_no_gaps:
        return "review_no_gaps"
    if saw_review:
        return "review_feedback_present"
    return None


def _classify_convergence(status: str | None, phase: str | None) -> str | None:
    if not status and not phase:
        return None
    if status == "pass" and phase == "complete":
        return "converged"
    if status == "error" and phase == "dsl_validation":
        return "validation_not_converged"
    if status in {"fail", "error"}:
        return "not_converged"
    return "unknown"


def _classify_llm_timings(timings) -> Counter[str]:
    calls: Counter[str] = Counter()
    for timing in timings:
        stage = _get(timing, "stage")
        detail = _get(timing, "detail") or ""
        if stage == "dsl_generation":
            calls["initial_generation"] += 1
        elif stage == "fidelity_review":
            calls["fidelity_review"] += 1
        elif stage == "assumptions_summary":
            calls["assumptions_summary"] += 1
        elif stage == "repair_loop" and detail.startswith("review repair "):
            calls["fidelity_repair"] += 1
        elif stage == "repair_loop":
            calls["validation_repair"] += 1
    return calls


def _count_dsl_lines(result, dsl_artifacts: Mapping[str, str | Path | None] | None) -> dict[str, int]:
    selected: dict[str, str | Path | None] = {}
    if dsl_artifacts is None:
        run_dir = _get(result, "run_dir")
        if run_dir:
            selected["initial"] = Path(run_dir) / "initial.dsl.py"
        selected["final"] = _get(result, "dsl_path")
        for attempt in _get(result, "attempts") or []:
            attempt_no = _get(attempt, "attempt")
            selected[f"attempt_{attempt_no}"] = _get(attempt, "candidate_path")
            selected[f"review_repair_{attempt_no}"] = _get(attempt, "review_repair_path")
    else:
        selected.update(dsl_artifacts)

    counts: dict[str, int] = {}
    for label, maybe_path in selected.items():
        if not maybe_path:
            continue
        path = Path(maybe_path)
        if path.exists() and path.is_file():
            counts[label] = len(path.read_text(encoding="utf-8").splitlines())
    return {label: counts[label] for label in sorted(counts)}
