# Architecture

invari-spec uses a narrow pipeline:

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

- `invari_spec.semantic_dsl`: parser, typed model, and TLA+/CFG lowerer
- `invari_spec.pipeline`: markdown conversion, TLC execution, and report rendering
- `invari_spec.cli`: public `invari-spec check` entrypoint
