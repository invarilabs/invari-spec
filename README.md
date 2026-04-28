# Speclens

Speclens helps you debug workflows, design docs, and requirements by turning prose specifications into executable models and checking them for gaps such as missing fallbacks, unreachable success states, infinite retry loops, and fairness-sensitive liveness failures.

Speclens is an OSS spec-debugging tool. It reads workflow-style markdown, builds a constrained behavioral model, runs executable checks, and returns high-signal bug reports with traces. The goal is not to prove entire systems correct; it is to make ambiguous or incomplete specs easier to reason about before they become code.

## What it does

- converts spec markdown into a constrained semantic DSL
- validates the DSL before lowering anything to TLA+
- runs TLC on the generated model
- preserves attempts, repairs, traces, and result metadata
- flags underspecification and fairness-sensitive liveness failures separately from ordinary bugs

## Quick start

```bash
./scripts/setup.sh
speclens check --file examples/workflow_retry_with_fallback/SPEC.md --dsl-file examples/workflow_retry_with_fallback/expected.dsl.py --run-tlc
```

The setup script installs the Python package and downloads `tla2tools.jar` into `third_party/`. Speclens uses that path by default.

If you want to override the defaults, the setup script supports:

- `PYTHON_BIN=/path/to/python ./scripts/setup.sh`
- `TLA_JAR_PATH=/custom/path/tla2tools.jar ./scripts/setup.sh`
- `TLA_JAR_URL=https://.../tla2tools.jar ./scripts/setup.sh`

You can still override jar resolution manually with:

- `speclens check --tla-jar-path /path/to/tla2tools.jar --file path/to/SPEC.md`

For LLM-backed markdown conversion, install the optional dependencies and set either `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`.

## CLI

```bash
speclens check --file path/to/SPEC.md
```

Useful flags:

- `--dsl-file <path>` resume from an existing DSL file
- `--format text|json`
- `--output-dir <path>`
- `--run-tlc` / `--no-run-tlc`
- `--model <llm-model>`
- `--max-attempts <n>`

## Examples

- `examples/workflow_retry_with_fallback`
- `examples/missing_fallback`
- `examples/infinite_retry`
- `examples/unreachable_success`

## Development

```bash
python3 -m unittest discover -s tests
```
