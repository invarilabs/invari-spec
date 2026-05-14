# invari-spec

## Commands

- **Run tests:** `.venv/bin/python -m unittest discover -s tests`
- **Run pipeline:** `.venv/bin/invari-spec check --file <spec.md> [options]`
- **TLA+ jar:** `/Users/kaungmyathtaywin/.vscode/extensions/tlaplus.vscode-ide-2026.5.121710/tools/tla2tools.jar` (from the VS Code TLA+ extension)
  - Pass via `--tla-jar-path` when running the pipeline with `--run-tlc`

## Commit conventions

- Use conventional commits: `type: Message`
- Start the message with a capital letter: `fix: Enforce X` not `fix: enforce X`
- Keep messages short and single-line
- No `Co-Authored-By` trailer
- When fixing a Linear ticket, add a footer `Refs: INV-XX` on a blank line after the subject
- Examples:
  - `fix: Enforce AnyOf(Bool) for frozen external bool outcome fields`
  - `feat: Add dead-branch detection for frozen outcomes`
  - `refactor: Extract validation helpers into ast_parser`
  - Multi-line with footer:
    ```
    feat: Enable deadlock detection with terminal-state Termination guard

    Refs: INV-55
    ```
