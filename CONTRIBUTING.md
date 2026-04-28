# Contributing

Keep changes scoped to the public spec-checking surface:

- semantic DSL parsing and lowering
- markdown-to-DSL generation and repair
- TLC execution and reporting
- curated examples and docs

Before opening a PR:

1. Run `python3 -m unittest discover -s tests`.
2. Run `python3 -m speclens.cli check --file examples/workflow_retry_with_fallback/SPEC.md --dsl-file examples/workflow_retry_with_fallback/expected.dsl.py --no-run-tlc`.
3. Avoid introducing repo-local paths, internal ticket references, or generated artifacts.
