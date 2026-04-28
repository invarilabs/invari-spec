# Architecture

Speclens uses a narrow pipeline:

```text
spec markdown
  -> candidate semantic DSL
  -> deterministic normalization
  -> strict DSL validation
  -> TLA+ lowering
  -> TLC execution
  -> classified report with traces
```

The trusted boundary is the semantic DSL parser, not the LLM. Raw LLM output is never lowered directly to TLA+.

The repo is organized around three layers:

- `speclens.semantic_dsl`: parser, typed model, and TLA+/CFG lowerer
- `speclens.pipeline`: markdown conversion, TLC execution, and report rendering
- `speclens.cli`: public `speclens check` entrypoint
