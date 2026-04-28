from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from speclens.pipeline import MarkdownToTlaRequest, convert_markdown_to_tla


ROOT = Path(__file__).resolve().parents[1]


class ExampleSmokeTest(unittest.TestCase):
    def test_examples_resume_mode_generate_expected_artifacts(self) -> None:
        examples = (
            "workflow_retry_with_fallback",
            "missing_fallback",
            "infinite_retry",
            "unreachable_success",
        )
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for example in examples:
                case_dir = ROOT / "examples" / example
                result = convert_markdown_to_tla(
                    MarkdownToTlaRequest(
                        input_path=case_dir / "SPEC.md",
                        generated_root=root / "generated",
                        dsl_file=case_dir / "expected.dsl.py",
                        run_tlc=False,
                        cwd=root,
                    )
                )

                self.assertEqual(result.status, "pass", example)
                self.assertTrue(result.dsl_path and Path(result.dsl_path).exists(), example)
                self.assertTrue(result.tla_path and Path(result.tla_path).exists(), example)
                self.assertTrue(result.cfg_path and Path(result.cfg_path).exists(), example)
