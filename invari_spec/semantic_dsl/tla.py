from __future__ import annotations

import re

from invari_spec.semantic_dsl.errors import DslLoweringError
from invari_spec.semantic_dsl.model import (
    AnyOfExpr,
    BoolType,
    CallExpr,
    CollectionType,
    CompletionRequiresDecl,
    EntityDecl,
    EnumType,
    Expr,
    FieldRef,
    ForbiddenDecl,
    IntRangeType,
    IntSetType,
    IntType,
    LiteralExpr,
    NamedType,
    Ref,
    RefExpr,
    SetFieldUpdate,
    SetUpdate,
    TypeRef,
    UnchangedUpdate,
    Update,
    VarDecl,
    VarRef,
    WorkflowModel,
)


def lower_to_tla(model: WorkflowModel, *, module_name: str | None = None) -> str:
    lowering = _Lowerer(model, module_name=module_name)
    return lowering.lower()


def build_cfg(model: WorkflowModel) -> str:
    lowering = _Lowerer(model)
    invariant_names = ["TypeOk"]
    invariant_names.extend(_operator_name(inv.name) for inv in model.invariants)
    invariant_names.extend(_operator_name(f.name) for f in model.forbiddens if not lowering.contains_transition_ref(f.predicate))

    property_names = [_operator_name(obligation.name) for obligation in model.obligations]
    property_names.extend(
        _operator_name(forbidden.name)
        for forbidden in model.forbiddens
        if lowering.contains_transition_ref(forbidden.predicate)
    )
    property_names.extend(_operator_name(comp.name) for comp in model.completion_requires)
    lines = ["SPECIFICATION Spec", "INVARIANTS", *invariant_names]
    if property_names:
        lines.extend(["PROPERTIES", *property_names])
    lines.append("")
    return "\n".join(lines)


class _Lowerer:
    def __init__(self, model: WorkflowModel, *, module_name: str | None = None) -> None:
        self.model = model
        self.module_name = _module_name(module_name or model.name)

    def lower(self) -> str:
        if not self.model.actions:
            raise DslLoweringError("cannot lower DSL model without actions")

        terminal = self._terminal_enum_conditions()

        lines: list[str] = [
            f"---- MODULE {self.module_name} ----",
            "EXTENDS Integers, FiniteSets, Sequences, TLC",
            "",
            *self._domain_operators(),
            *self._variables(),
            "",
            *self._type_ok(),
            "",
            *self._init(),
            "",
        ]

        for action in self.model.actions:
            lines.extend(self._action(action_name=action.name, changes=action.changes, requires=action.requires))
            lines.append("")

        if terminal:
            lines.extend(self._termination_operator(terminal))
            lines.append("")

        lines.extend(self._next(terminal))
        spec_clauses = ["Init /\\ [][Next]_vars", *self._fairness_clauses()]
        lines.extend(["", "Spec ==", *(f"  /\\ {clause}" if idx else f"  {clause}" for idx, clause in enumerate(spec_clauses)), ""])
        lines.extend(self._properties())
        lines.extend(["====", ""])
        return "\n".join(lines)

    def contains_transition_ref(self, expr: Expr) -> bool:
        if isinstance(expr, CallExpr):
            return expr.op in {"Changed", "Unchanged"} or any(self.contains_transition_ref(arg) for arg in expr.args)
        return False

    def _domain_operators(self) -> list[str]:
        lines: list[str] = []
        seen_named: set[str] = set()
        for entity in self.model.entities.values():
            for field_name, type_ref in entity.fields.items():
                lines.extend(self._domain_for_type(f"{entity.name}_{field_name}", type_ref, seen_named))
        for var in self.model.vars.values():
            lines.extend(self._domain_for_type(var.name, var.type_ref, seen_named))
        if lines:
            lines.append("")
        return lines

    def _domain_for_type(self, prefix: str, type_ref: TypeRef, seen_named: set[str]) -> list[str]:
        if isinstance(type_ref, EnumType):
            return [f"{_operator_name(prefix + '_Domain')} == {{{', '.join(_literal(v) for v in type_ref.values)}}}"]
        if isinstance(type_ref, CollectionType):
            return self._domain_for_type(prefix, type_ref.item_type, seen_named)
        if isinstance(type_ref, NamedType) and type_ref.name not in seen_named:
            seen_named.add(type_ref.name)
            domain = _operator_name(type_ref.name + "_Domain")
            return [f"{domain} == {{{_literal(type_ref.name + '_1')}, {_literal(type_ref.name + '_2')}}}"]
        return []

    def _variables(self) -> list[str]:
        names = list(self.model.entities) + list(self.model.vars)
        return [f"VARIABLES {', '.join(names)}", "", f"vars == << {', '.join(names)} >>"]

    def _type_ok(self) -> list[str]:
        lines = ["TypeOk =="]
        clauses: list[str] = []
        for entity in self.model.entities.values():
            fields = ", ".join(f"{field}: {self._type_expr(type_ref, entity.name, field)}" for field, type_ref in entity.fields.items())
            clauses.append(f"{entity.name} \\in [ {fields} ]")
        for var in self.model.vars.values():
            clauses.append(f"{var.name} \\in {self._type_expr(var.type_ref, var.name, None)}")
        if not clauses:
            return ["TypeOk == TRUE"]
        lines.extend(f"  /\\ {clause}" for clause in clauses)
        return lines

    def _type_expr(self, type_ref: TypeRef, owner: str, field: str | None) -> str:
        if isinstance(type_ref, BoolType):
            return "BOOLEAN"
        if isinstance(type_ref, IntType):
            return "Int"
        if isinstance(type_ref, IntRangeType):
            return f"{type_ref.lo}..{type_ref.hi}"
        if isinstance(type_ref, IntSetType):
            return "{" + ", ".join(str(v) for v in type_ref.values) + "}"
        if isinstance(type_ref, EnumType):
            prefix = f"{owner}_{field}" if field else owner
            return _operator_name(prefix + "_Domain")
        if isinstance(type_ref, CollectionType):
            return f"SUBSET {self._type_expr(type_ref.item_type, owner, field)}"
        if isinstance(type_ref, NamedType):
            return _operator_name(type_ref.name + "_Domain")
        raise DslLoweringError(f"unsupported type: {type_ref!r}")

    def _init(self) -> list[str]:
        lines = ["Init =="]
        clauses = self._init_clauses()
        lines.extend(f"  /\\ {clause}" for clause in clauses)
        return lines

    def _init_clauses(self) -> list[str]:
        entity_values: dict[str, dict[str, Expr]] = {name: {} for name in self.model.entities}
        scalar_clauses: list[str] = []
        for expr in self.model.init:
            extracted = self._init_field_assignment(expr)
            if extracted is None:
                scalar_clauses.append(self._expr(expr))
                continue
            ref, value = extracted
            if isinstance(ref, FieldRef):
                entity_values.setdefault(ref.entity, {})[ref.field] = value
            else:
                if isinstance(value, AnyOfExpr):
                    scalar_clauses.append(f"{ref.name} \\in {self._type_expr(value.type_ref, ref.name, None)}")
                else:
                    scalar_clauses.append(f"{ref.name} = {self._expr(value)}")

        clauses: list[str] = []
        for entity_name, entity in self.model.entities.items():
            fields = entity_values.get(entity_name, {})
            if fields:
                missing = [field for field in entity.fields if field not in fields]
                if missing:
                    raise DslLoweringError(
                        f"cannot lower partial init for entity {entity_name}: missing {', '.join(missing)}"
                    )
                if any(isinstance(v, AnyOfExpr) for v in fields.values()):
                    field_sets = ", ".join(
                        f"{field}: {self._init_value_as_set(fields[field], entity_name, field)}"
                        for field in entity.fields
                    )
                    clauses.append(f"{entity_name} \\in [{field_sets}]")
                else:
                    record_fields = ", ".join(f"{field} |-> {self._expr(fields[field])}" for field in entity.fields)
                    clauses.append(f"{entity_name} = [{record_fields}]")
        clauses.extend(scalar_clauses)
        return clauses

    def _init_value_as_set(self, value: Expr, entity_name: str, field_name: str) -> str:
        if isinstance(value, AnyOfExpr):
            return self._type_expr(value.type_ref, entity_name, field_name)
        return f"{{{self._expr(value)}}}"

    def _init_field_assignment(self, expr: Expr) -> tuple[Ref, Expr] | None:
        if not isinstance(expr, CallExpr) or expr.op != "Eq" or len(expr.args) != 2:
            return None
        lhs, rhs = expr.args
        if not isinstance(lhs, RefExpr):
            return None
        return lhs.ref, rhs

    def _action(self, *, action_name: str, changes: tuple[Update, ...], requires: tuple[Expr, ...]) -> list[str]:
        op_name = _operator_name(action_name)
        lines = [f"{op_name} =="]
        clauses = [self._expr(expr) for expr in requires]
        clauses.extend(self._updates(changes))

        unchanged = self._unchanged_vars(changes)
        if unchanged:
            clauses.append(f"UNCHANGED << {', '.join(unchanged)} >>")
        if not clauses:
            clauses.append("TRUE")
        lines.extend(f"  /\\ {clause}" for clause in clauses)
        return lines

    def _updates(self, changes: tuple[Update, ...]) -> list[str]:
        scalar_updates: list[str] = []
        entity_updates: dict[str, list[SetFieldUpdate]] = {}
        for update in changes:
            if isinstance(update, SetUpdate):
                scalar_updates.append(f"{update.target.name}' = {self._expr(update.value)}")
            elif isinstance(update, SetFieldUpdate):
                entity_updates.setdefault(update.target.entity, []).append(update)
            elif isinstance(update, UnchangedUpdate):
                continue

        lines = list(scalar_updates)
        for entity, updates in entity_updates.items():
            excepts = ", ".join(f"!.{update.target.field} = {self._expr(update.value)}" for update in updates)
            lines.append(f"{entity}' = [{entity} EXCEPT {excepts}]")
        return lines

    def _unchanged_vars(self, changes: tuple[Update, ...]) -> list[str]:
        changed: set[str] = set()
        for update in changes:
            if isinstance(update, SetUpdate):
                changed.add(update.target.name)
            elif isinstance(update, SetFieldUpdate):
                changed.add(update.target.entity)
        names = list(self.model.entities) + list(self.model.vars)
        return [name for name in names if name not in changed]

    def _next(self, terminal: list[tuple[str, str, str]] | None = None) -> list[str]:
        lines = ["Next =="]
        for action in self.model.actions:
            lines.append(f"  \\/ {_operator_name(action.name)}")
        if terminal:
            lines.append("  \\/ Termination")
        return lines

    def _termination_operator(self, terminal: list[tuple[str, str, str]]) -> list[str]:
        lines = ["Termination =="]
        for entity_name, field_name, value in terminal:
            lines.append(f"  \\/ ({entity_name}.{field_name} = {_literal(value)} /\\ UNCHANGED vars)")
        return lines

    def _terminal_enum_conditions(self) -> list[tuple[str, str, str]]:
        required_from: set[tuple[str, str, str]] = set()
        required_fields: set[tuple[str, str]] = set()
        for action in self.model.actions:
            for expr in action.requires:
                self._collect_eq_field_enum(expr, required_from, required_fields)

        changed_fields: set[tuple[str, str]] = set()
        for action in self.model.actions:
            for update in action.changes:
                if isinstance(update, SetFieldUpdate):
                    entity = self.model.entities.get(update.target.entity)
                    if entity and isinstance(entity.fields.get(update.target.field), EnumType):
                        changed_fields.add((update.target.entity, update.target.field))

        progress_fields = required_fields & changed_fields
        terminal: list[tuple[str, str, str]] = []
        for entity_name, field_name in sorted(progress_fields):
            entity = self.model.entities[entity_name]
            type_ref = entity.fields[field_name]
            if isinstance(type_ref, EnumType):
                for value in type_ref.values:
                    if (entity_name, field_name, value) not in required_from:
                        terminal.append((entity_name, field_name, value))
        return terminal

    def _collect_eq_field_enum(
        self,
        expr: Expr,
        out_values: set[tuple[str, str, str]],
        out_fields: set[tuple[str, str]],
    ) -> None:
        if isinstance(expr, CallExpr):
            if expr.op == "Eq" and len(expr.args) == 2:
                lhs, rhs = expr.args
                if (
                    isinstance(lhs, RefExpr)
                    and isinstance(lhs.ref, FieldRef)
                    and isinstance(rhs, LiteralExpr)
                    and isinstance(rhs.value, str)
                ):
                    ref = lhs.ref
                    entity = self.model.entities.get(ref.entity)
                    if entity and isinstance(entity.fields.get(ref.field), EnumType):
                        out_fields.add((ref.entity, ref.field))
                        out_values.add((ref.entity, ref.field, rhs.value))
            for arg in expr.args:
                self._collect_eq_field_enum(arg, out_values, out_fields)

    def _fairness_clauses(self) -> list[str]:
        clauses: list[str] = []
        for action in self.model.actions:
            if action.fairness == "weak":
                clauses.append(f"WF_vars({_operator_name(action.name)})")
            elif action.fairness == "strong":
                clauses.append(f"SF_vars({_operator_name(action.name)})")
        return clauses

    def _properties(self) -> list[str]:
        lines: list[str] = []
        for inv in self.model.invariants:
            lines.extend([f"{_operator_name(inv.name)} ==", f"  {self._expr(inv.predicate)}", ""])
        for comp in self.model.completion_requires:
            lines.extend(
                [
                    "\\* Completion requirements are liveness properties, not same-state invariants.",
                    f"{_operator_name(comp.name)} ==",
                    f"  []({self._expr(comp.outcome)} => <>{self._expr(comp.condition)})",
                    "",
                ]
            )
        for forbidden in self.model.forbiddens:
            if not self.contains_transition_ref(forbidden.predicate):
                lines.extend([f"{_operator_name(forbidden.name)} ==", f"  ~({self._expr(forbidden.predicate)})", ""])
            else:
                lines.extend(
                    [
                        f"{_operator_name(forbidden.name)} ==",
                        f"  [](~({self._expr(forbidden.predicate)}))",
                        "",
                    ]
                )
        for obligation in self.model.obligations:
            lines.extend(
                [
                    "\\* Obligation liveness checks may require fairness assumptions for non-vacuous results.",
                    f"{_operator_name(obligation.name)} ==",
                    f"  []({self._expr(obligation.trigger)} => <>{self._expr(obligation.must_eventually)})",
                    "",
                ]
            )
        if self._uses_collection():
            lines.extend(
                [
                    "\\* V1 Collection values lower to finite sets; duplicate tracking is represented by set semantics.",
                    "",
                ]
            )
        return lines

    def _expr(self, expr: Expr) -> str:
        if isinstance(expr, LiteralExpr):
            return _literal(expr.value)
        if isinstance(expr, RefExpr):
            return self._ref(expr.ref, primed=False)
        if isinstance(expr, CallExpr):
            return self._call_expr(expr)
        if isinstance(expr, AnyOfExpr):
            raise DslLoweringError("AnyOf(...) is only valid as the value in init Eq(...) assignments")
        raise DslLoweringError(f"unsupported expression: {expr!r}")

    def _call_expr(self, expr: CallExpr) -> str:
        args = [self._expr(arg) for arg in expr.args]
        op = expr.op
        if op == "Eq":
            return _binary(args, "=")
        if op == "And":
            return _variadic(args, "/\\")
        if op == "Or":
            return _variadic(args, "\\/")
        if op == "Not":
            return f"~({self._one(args, op)})"
        if op == "Implies":
            return _binary(args, "=>")
        if op == "Add":
            return _binary(args, "+")
        if op == "Sub":
            return _binary(args, "-")
        if op == "Lt":
            return _binary(args, "<")
        if op == "Le":
            return _binary(args, "<=")
        if op == "Gt":
            return _binary(args, ">")
        if op == "Ge":
            return _binary(args, ">=")
        if op == "Contains":
            return f"({args[1]} \\in {args[0]})"
        if op == "Count":
            return f"Cardinality({self._one(args, op)})"
        if op == "CountUnique":
            return f"Cardinality({self._one(args, op)})"
        if op == "AddItem":
            return f"({args[0]} \\cup {{{args[1]}}})"
        if op == "RemoveItem":
            return f"({args[0]} \\ {{{args[1]}}})"
        if op == "Changed":
            ref = self._one_ref(expr, op)
            return f"{self._ref(ref, primed=True)} # {self._ref(ref, primed=False)}"
        if op == "Unchanged":
            ref = self._one_ref(expr, op)
            return f"{self._ref(ref, primed=True)} = {self._ref(ref, primed=False)}"
        raise DslLoweringError(f"unsupported expression op: {op}")

    def _one(self, args: list[str], op: str) -> str:
        if len(args) != 1:
            raise DslLoweringError(f"{op}(...) expects one argument")
        return args[0]

    def _one_ref(self, expr: CallExpr, op: str) -> Ref:
        if len(expr.args) != 1 or not isinstance(expr.args[0], RefExpr):
            raise DslLoweringError(f"{op}(...) expects one Var(...) or Field(...) reference")
        return expr.args[0].ref

    def _ref(self, ref: Ref, *, primed: bool) -> str:
        suffix = "'" if primed else ""
        if isinstance(ref, VarRef):
            return f"{ref.name}{suffix}"
        if isinstance(ref, FieldRef):
            return f"{ref.entity}{suffix}.{ref.field}"
        raise DslLoweringError(f"unsupported reference: {ref!r}")

    def _uses_collection(self) -> bool:
        for entity in self.model.entities.values():
            if any(_type_contains_collection(t) for t in entity.fields.values()):
                return True
        return any(_type_contains_collection(v.type_ref) for v in self.model.vars.values())


def _type_contains_collection(type_ref: TypeRef) -> bool:
    if isinstance(type_ref, CollectionType):
        return True
    return False


def _literal(value: str | int | bool) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int):
        return str(value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _binary(args: list[str], op: str) -> str:
    if len(args) != 2:
        raise DslLoweringError(f"{op} expression expects two arguments")
    return f"({args[0]} {op} {args[1]})"


def _variadic(args: list[str], op: str) -> str:
    if not args:
        return "TRUE" if op == "/\\" else "FALSE"
    if len(args) == 1:
        return args[0]
    return "(" + f" {op} ".join(args) + ")"


def _module_name(value: str) -> str:
    candidate = _operator_name(value)
    if not candidate:
        return "SemanticDslModel"
    if candidate[0].isdigit():
        return f"M_{candidate}"
    return candidate


def _operator_name(value: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", value)
    candidate = "".join(part[:1].upper() + part[1:] for part in parts if part)
    if not candidate:
        return "Generated"
    if candidate[0].isdigit():
        return f"M_{candidate}"
    return candidate
