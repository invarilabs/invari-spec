from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SanityResult:
    tla_path: Path
    cfg_path: Path | None
    updated: bool
    warnings: list[str]
    errors: list[str]


_MODULE_RE = re.compile(r"^\s*-+\s*MODULE\s+([A-Za-z0-9_]+)\s*-+\s*$")
_OP_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*==\s*(.*)$")
_PRIMED_VAR_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)'(?![A-Za-z0-9_])")
_IDENTIFIER_RE = re.compile(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)(?![A-Za-z0-9_])")
_RANGE_RE = re.compile(r"\b\d+\s*\.\.\s*\d+\b")
_SEQUENCE_RE = re.compile(r"\b(Append|Len|Seq|Head|Tail|SubSeq)\s*\(|\\o\b")
_UNCHANGED_RE = re.compile(r"UNCHANGED\s*<<\s*([^>]+?)\s*>>")
_PYTHON_UPDATE_COMMENT = "\\* Updated by python sanity checker"


def sanitize_pc_outputs(paths: list[Path]) -> list[Path]:
    resolved_paths = [path.resolve() for path in paths]
    cfg_by_key: dict[str, list[Path]] = {}
    tla_paths: list[Path] = []

    for path in resolved_paths:
        if path.suffix == ".tla":
            tla_paths.append(path)
        elif path.suffix == ".cfg":
            cfg_by_key.setdefault(_stem_key(path.stem), []).append(path)

    old_to_new: dict[Path, Path] = {}
    seen_tla: set[Path] = set()
    for tla_path in tla_paths:
        if tla_path in seen_tla:
            continue
        seen_tla.add(tla_path)
        cfg_candidates = cfg_by_key.get(_stem_key(tla_path.stem), [])
        cfg_path = cfg_candidates[0] if cfg_candidates else _find_cfg_for_tla(tla_path)
        result = sanitize_tla_cfg_pair(tla_path, cfg_path)
        if cfg_path is not None and result.cfg_path is not None:
            old_to_new[cfg_path.resolve()] = result.cfg_path.resolve()

    out: list[Path] = []
    for path in resolved_paths:
        out.append(old_to_new.get(path, path))
    return out


def sanitize_tla_cfg_pair(tla_path: Path, cfg_path: Path | None) -> SanityResult:
    tla_path = tla_path.resolve()
    cfg_path = cfg_path.resolve() if cfg_path is not None else None
    warnings: list[str] = []
    errors: list[str] = []
    updated = False

    original_tla = tla_path.read_text(encoding="utf-8")
    rewritten_tla, tla_warnings = _sanitize_tla_text(original_tla, tla_path.stem)
    warnings.extend(tla_warnings)
    if rewritten_tla != original_tla:
        tla_path.write_text(_ensure_trailing_newline(_with_python_update_comment_tla(rewritten_tla)), encoding="utf-8")
        updated = True

    final_cfg_path = cfg_path
    if cfg_path is not None and cfg_path.exists():
        expected_cfg = cfg_path.with_name(f"{tla_path.stem}.cfg")
        original_cfg = cfg_path.read_text(encoding="utf-8")
        rewritten_cfg = _rewrite_cfg_text(original_cfg, cfg_path.stem, tla_path.stem)
        if cfg_path != expected_cfg:
            same_target = expected_cfg.resolve() == cfg_path.resolve()
            target_path = cfg_path if same_target else expected_cfg
            target_path.write_text(_ensure_trailing_newline(_with_python_update_comment_cfg(rewritten_cfg)), encoding="utf-8")
            if not same_target:
                cfg_path.unlink(missing_ok=True)
            final_cfg_path = target_path
            updated = True
        elif rewritten_cfg != original_cfg:
            cfg_path.write_text(_ensure_trailing_newline(_with_python_update_comment_cfg(rewritten_cfg)), encoding="utf-8")
            updated = True

    return SanityResult(
        tla_path=tla_path,
        cfg_path=final_cfg_path,
        updated=updated,
        warnings=warnings,
        errors=errors,
    )


def _sanitize_tla_text(text: str, module_name: str) -> tuple[str, list[str]]:
    warnings: list[str] = []
    lines = text.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and lines[-1].strip() == "====":
        lines.pop()

    lines = _normalize_module_header(lines, module_name)
    lines = _normalize_extends(lines)

    variables, lines = _extract_variables(lines)
    for primed_name in _find_primed_variables("\n".join(lines)):
        if primed_name not in variables:
            variables.append(primed_name)

    lines = _set_variables_line(lines, variables)
    lines = _upsert_vars_definition(lines, variables)
    lines, init_warnings = _upsert_init_block(lines, variables)
    warnings.extend(init_warnings)
    lines, action_warnings = _patch_actions(lines, variables)
    warnings.extend(action_warnings)
    lines, reorder_warnings = _reorder_operator_blocks(lines)
    warnings.extend(reorder_warnings)

    body = "\n".join(lines).rstrip()
    return f"{body}\n====", warnings


def _normalize_module_header(lines: list[str], module_name: str) -> list[str]:
    out: list[str] = []
    saw_header = False
    for line in lines:
        if _MODULE_RE.match(line):
            if not saw_header:
                saw_header = True
            continue
        out.append(line)

    while out and not out[0].strip():
        out.pop(0)
    return [f"---- MODULE {module_name} ----", *out]


def _normalize_extends(lines: list[str]) -> list[str]:
    collected: list[str] = []
    kept: list[str] = []
    for idx, line in enumerate(lines):
        if idx == 0:
            kept.append(line)
            continue
        match = re.match(r"^\s*EXTENDS\s+(.+)$", line)
        if match:
            collected.extend(_split_csv(match.group(1)))
            continue
        kept.append(line)

    text = "\n".join(kept)
    required: list[str] = []
    if _RANGE_RE.search(text):
        required.append("Naturals")
    if _SEQUENCE_RE.search(text):
        required.append("Sequences")

    for name in required:
        if name not in collected:
            collected.append(name)

    if not collected:
        return kept

    return [kept[0], f"EXTENDS {', '.join(collected)}", *kept[1:]]


def _extract_variables(lines: list[str]) -> tuple[list[str], list[str]]:
    out: list[str] = []
    variables: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = re.match(r"^\s*VARIABLES\b(.*)$", line)
        if not match:
            out.append(line)
            idx += 1
            continue

        remainder = match.group(1)
        parts = [remainder]
        idx += 1
        while idx < len(lines):
            candidate = lines[idx]
            stripped = candidate.strip()
            if (
                not stripped
                or stripped == "===="
                or stripped.startswith("EXTENDS")
                or stripped.startswith("---- MODULE")
                or _OP_RE.match(stripped)
            ):
                break
            parts.append(candidate)
            idx += 1

        for value in _split_csv(" ".join(parts)):
            if value not in variables:
                variables.append(value)
        continue

    return variables, out


def _set_variables_line(lines: list[str], variables: list[str]) -> list[str]:
    variables_line = f"VARIABLES {', '.join(variables)}" if variables else "VARIABLES"
    insert_at = 1
    if len(lines) > 1 and lines[1].startswith("EXTENDS "):
        insert_at = 2
    return lines[:insert_at] + [variables_line] + lines[insert_at:]


def _upsert_vars_definition(lines: list[str], variables: list[str]) -> list[str]:
    block = f"vars == << {', '.join(variables)} >>" if variables else "vars == << >>"
    blocks = _split_operator_blocks(lines)
    existing = next((idx for idx, (name, _) in enumerate(blocks) if name == "vars"), None)
    if existing is not None:
        blocks[existing] = ("vars", [block])
        return _join_operator_blocks(blocks)

    insert_at = _variables_insert_index(lines) + 1
    return lines[:insert_at] + [block] + lines[insert_at:]


def _upsert_init_block(lines: list[str], variables: list[str]) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    blocks = _split_operator_blocks(lines)
    block_names = [name for name, _ in blocks]
    init_idx = next((idx for idx, (name, _) in enumerate(blocks) if name == "Init"), None)
    if init_idx is None:
        init_lines = ["Init =="]
        for var_name in variables:
            init_lines.append(f"    /\\ {var_name} \\in BOOLEAN")
            warnings.append(f"Init added placeholder BOOLEAN domain for `{var_name}`")
        insert_pos = next((idx for idx, name in enumerate(block_names) if name not in {"", "vars"}), len(blocks))
        blocks.insert(insert_pos, ("Init", init_lines))
        return _join_operator_blocks(blocks), warnings

    _, init_lines = blocks[init_idx]
    normalized_init = _normalize_operator_body_lines(init_lines)
    init_text = "\n".join(normalized_init)
    missing = []
    for var_name in variables:
        if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(var_name)}\s*(=|\\in|\\notin)", init_text):
            missing.append(var_name)
    for var_name in missing:
        normalized_init.append(f"    /\\ {var_name} \\in BOOLEAN")
        warnings.append(f"Init added placeholder BOOLEAN domain for `{var_name}`")
    blocks[init_idx] = ("Init", normalized_init)
    return _join_operator_blocks(blocks), warnings


def _patch_actions(lines: list[str], variables: list[str]) -> tuple[list[str], list[str]]:
    warnings: list[str] = []
    blocks = _split_operator_blocks(lines)
    out_blocks: list[tuple[str, list[str]]] = []

    for name, block_lines in blocks:
        if name in {"vars", "Init", "Next", "Spec"}:
            out_blocks.append((name, block_lines))
            continue

        block_text = "\n".join(block_lines)
        primed = [var_name for var_name in variables if re.search(rf"(?<![A-Za-z0-9_]){re.escape(var_name)}'(?![A-Za-z0-9_])", block_text)]
        if not primed:
            out_blocks.append((name, block_lines))
            continue

        if _is_complex_action(block_text):
            warnings.append(f"Skipped UNCHANGED patch for complex action `{name}`")
            out_blocks.append((name, block_lines))
            continue

        normalized_lines = _normalize_operator_body_lines(block_lines)
        missing = [var_name for var_name in variables if var_name not in primed]
        if missing:
            normalized_lines = _merge_unchanged(normalized_lines, missing)
        out_blocks.append((name, normalized_lines))

    return _join_operator_blocks(out_blocks), warnings


def _merge_unchanged(lines: list[str], missing: list[str]) -> list[str]:
    out: list[str] = []
    replaced = False
    for line in lines:
        match = _UNCHANGED_RE.search(line)
        if not match:
            out.append(line)
            continue

        existing = _split_csv(match.group(1))
        for var_name in missing:
            if var_name not in existing:
                existing.append(var_name)
        prefix = line[: match.start()]
        out.append(f"{prefix}UNCHANGED << {', '.join(existing)} >>")
        replaced = True

    if replaced:
        return out

    if len(lines) == 1 and "==" in lines[0]:
        return [lines[0], f"    /\\ UNCHANGED << {', '.join(missing)} >>"]

    out.append(f"    /\\ UNCHANGED << {', '.join(missing)} >>")
    return out


def _normalize_operator_body_lines(lines: list[str]) -> list[str]:
    if not lines:
        return lines
    header = lines[0]
    match = _OP_RE.match(header.strip())
    if not match:
        return lines
    inline_tail = match.group(2).strip()
    body = [header.split("==", 1)[0].rstrip() + " =="]
    if inline_tail:
        if inline_tail.startswith("/\\"):
            body.append(f"    {inline_tail}")
        else:
            body.append(f"    /\\ {inline_tail}")
    for line in lines[1:]:
        stripped = line.strip()
        if stripped:
            body.append(line)
    return body


def _split_operator_blocks(lines: list[str]) -> list[tuple[str, list[str]]]:
    preamble: list[str] = []
    blocks: list[tuple[str, list[str]]] = []
    current_name = ""
    current_lines: list[str] | None = None

    for line in lines:
        stripped = line.strip()
        match = _OP_RE.match(stripped)
        if match and not stripped.startswith("---- MODULE"):
            if current_lines is not None:
                blocks.append((current_name, current_lines))
            current_name = match.group(1)
            current_lines = [line]
            continue

        if current_lines is None:
            preamble.append(line)
        else:
            current_lines.append(line)

    if current_lines is not None:
        blocks.append((current_name, current_lines))

    if preamble:
        return [("", preamble), *blocks]
    return blocks


def _join_operator_blocks(blocks: list[tuple[str, list[str]]]) -> list[str]:
    out: list[str] = []
    for idx, (_, block_lines) in enumerate(blocks):
        if not block_lines:
            continue
        if out and out[-1].strip() and block_lines[0].strip():
            out.append("")
        out.extend(block_lines)
    return out


def _reorder_operator_blocks(lines: list[str]) -> tuple[list[str], list[str]]:
    blocks = _split_operator_blocks(lines)
    if not blocks:
        return lines, []

    preamble = blocks[0] if blocks and blocks[0][0] == "" else None
    named_blocks = blocks[1:] if preamble is not None else blocks
    if len(named_blocks) < 2:
        return lines, []

    ordered_blocks, warnings = _topologically_order_blocks(named_blocks)
    out_blocks = [preamble] if preamble is not None and preamble[1] else []
    out_blocks.extend(ordered_blocks)
    return _join_operator_blocks(out_blocks), warnings


def _operator_dependencies(block_name: str, block_lines: list[str], defined_names: set[str]) -> set[str]:
    dependencies: set[str] = set()
    if not block_lines:
        return dependencies

    lines_to_scan = []
    if block_lines:
        header_match = _OP_RE.match(block_lines[0].strip())
        if header_match:
            inline_tail = header_match.group(2).strip()
            if inline_tail:
                lines_to_scan.append(inline_tail)
    lines_to_scan.extend(block_lines[1:])

    for line in lines_to_scan:
        for match in _IDENTIFIER_RE.finditer(line):
            name = match.group(1)
            if name == block_name or name not in defined_names:
                continue
            dependencies.add(name)
    return dependencies


def _operator_rank(name: str) -> tuple[int, str]:
    if name == "vars":
        return (0, name)
    if name == "Init":
        return (1, name)
    if name.startswith("Action_"):
        return (3, name)
    if name == "Next":
        return (4, name)
    if name.endswith("Invariant") or name in {"TypeOk", "Completed"}:
        return (5, name)
    if name == "Spec":
        return (6, name)
    return (2, name)


def _topologically_order_blocks(blocks: list[tuple[str, list[str]]]) -> tuple[list[tuple[str, list[str]]], list[str]]:
    warnings: list[str] = []
    defined_names = {name for name, _ in blocks if name}
    original_index = {name: idx for idx, (name, _) in enumerate(blocks)}
    dependencies: dict[str, set[str]] = {
        name: _operator_dependencies(name, block_lines, defined_names)
        for name, block_lines in blocks
        if name
    }

    indegree = {name: 0 for name in dependencies}
    dependents: dict[str, set[str]] = {name: set() for name in dependencies}
    for name, deps in dependencies.items():
        indegree[name] = len(deps)
        for dep in deps:
            dependents.setdefault(dep, set()).add(name)

    ready = sorted(
        [name for name, degree in indegree.items() if degree == 0],
        key=lambda item: (_operator_rank(item), original_index[item]),
    )
    ordered_names: list[str] = []

    while ready:
        current = ready.pop(0)
        ordered_names.append(current)
        for dependent in sorted(
            dependents.get(current, set()),
            key=lambda item: (_operator_rank(item), original_index[item]),
        ):
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=lambda item: (_operator_rank(item), original_index[item]))

    if len(ordered_names) != len(dependencies):
        cycle_names = [
            name for name, degree in indegree.items() if degree > 0
        ]
        ordered_cycle_names = sorted(cycle_names, key=lambda item: original_index[item])
        warnings.append(
            "Skipped reorder for cyclic operator references involving "
            + ", ".join(ordered_cycle_names)
        )
        return blocks, warnings

    block_by_name = {name: block for name, block in blocks}
    ordered_blocks = [(name, block_by_name[name]) for name in ordered_names]
    return ordered_blocks, warnings


def _variables_insert_index(lines: list[str]) -> int:
    for idx, line in enumerate(lines):
        if line.startswith("VARIABLES "):
            return idx
    return 1 if len(lines) > 1 and lines[1].startswith("EXTENDS ") else 0


def _find_primed_variables(text: str) -> list[str]:
    out: list[str] = []
    for match in _PRIMED_VAR_RE.finditer(text):
        name = match.group(1)
        if name not in out:
            out.append(name)
    return out


def _split_csv(text: str) -> list[str]:
    values: list[str] = []
    for part in text.split(","):
        value = part.strip()
        if value and value not in values:
            values.append(value)
    return values


def _rewrite_cfg_text(text: str, old_stem: str, new_stem: str) -> str:
    rewritten = text
    if old_stem != new_stem:
        rewritten = re.sub(rf"(?<![A-Za-z0-9_]){re.escape(old_stem)}(?![A-Za-z0-9_])", new_stem, rewritten)

    cfg_lines = rewritten.splitlines()
    while cfg_lines and not cfg_lines[-1].strip():
        cfg_lines.pop()
    if cfg_lines and cfg_lines[-1].strip() == "====":
        cfg_lines.pop()
    cfg_lines = [line for line in cfg_lines if line.strip() != "CHECK_DEADLOCK FALSE"]
    cfg_lines.append("CHECK_DEADLOCK FALSE")
    return "\n".join(cfg_lines)


def _with_python_update_comment_tla(text: str) -> str:
    lines = text.splitlines()
    if _PYTHON_UPDATE_COMMENT in lines:
        return text
    if lines and _MODULE_RE.match(lines[0]):
        return "\n".join([lines[0], _PYTHON_UPDATE_COMMENT, *lines[1:]])
    return "\n".join([_PYTHON_UPDATE_COMMENT, *lines]) if lines else _PYTHON_UPDATE_COMMENT


def _with_python_update_comment_cfg(text: str) -> str:
    lines = text.splitlines()
    if _PYTHON_UPDATE_COMMENT in lines:
        return text
    return "\n".join([_PYTHON_UPDATE_COMMENT, *lines]) if lines else _PYTHON_UPDATE_COMMENT


def _find_cfg_for_tla(tla_path: Path) -> Path | None:
    direct = tla_path.with_suffix(".cfg")
    if direct.exists():
        return direct

    tla_key = _stem_key(tla_path.stem)
    for candidate in tla_path.parent.glob("*.cfg"):
        if _stem_key(candidate.stem) == tla_key:
            return candidate
    return None


def _stem_key(stem: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", stem.lower())


def _is_complex_action(text: str) -> bool:
    normalized = text.upper()
    return any(token in normalized for token in (" LET ", " IN ", " IF ", " THEN ", " ELSE ", " CASE "))


def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else f"{text}\n"
