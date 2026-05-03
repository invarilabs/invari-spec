from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class CliCheckTest(unittest.TestCase):
    def test_cli_check_supports_json_output_and_resume_mode(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            output_dir = Path(td) / "generated"
            cmd = [
                sys.executable,
                "-m",
                "invari_spec.cli",
                "check",
                "--file",
                str(ROOT / "examples" / "workflow_retry_with_fallback" / "SPEC.md"),
                "--dsl-file",
                str(ROOT / "examples" / "workflow_retry_with_fallback" / "expected.dsl.py"),
                "--format",
                "json",
                "--output-dir",
                str(output_dir),
                "--no-run-tlc",
            ]
            proc = subprocess.run(cmd, cwd=ROOT, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertEqual(payload["status"], "pass")
            self.assertIn("bug_classes", payload)
            self.assertIn("underspecified_assumptions", payload)
