from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from speclens.semantic_dsl import build_cfg, lower_to_tla, parse_dsl_file
from speclens.pipeline import (
    MarkdownToTlaRequest,
    build_initial_markdown_to_dsl_prompt,
    build_minimal_dsl_repair_prompt,
    convert_markdown_to_tla,
    load_fixtures,
    render_result,
    normalize_common_dsl_syntax,
    normalize_llm_dsl_output,
)
from speclens.pipeline.markdown_to_dsl import _find_tla_jar


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOT = ROOT / "examples"


class FakeLLMClient:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.prompts: list[str] = []

    def generate(self, prompt: str, model: str | None = None, max_tokens: int = 16384) -> str:
        _ = model
        _ = max_tokens
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

    def test_repair_success_writes_attempts_fix_comments_and_tla(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient([INVALID_DSL, VALID_DSL])

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
            self.assertIn("# FIX attempt 2: initialized missing field task.retry_count", result.fix_comments)
            self.assertEqual(result.warnings, [])
            self.assertFalse(result.fairness_sensitive)
            self.assertEqual(result.liveness_classification, "not_applicable")

    def test_repair_attempt_without_fix_comment_is_rejected_before_later_success(self) -> None:
        valid_attempt_3 = VALID_DSL.replace("# FIX attempt 2:", "# FIX attempt 3:")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "SPEC.md"
            skill.write_text("# Spec: repair\n", encoding="utf-8")
            client = FakeLLMClient([INVALID_DSL, VALID_DSL_WITHOUT_FIX_COMMENT, valid_attempt_3])

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


if __name__ == "__main__":
    unittest.main()
