from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any

from invari_spec.semantic_dsl.errors import DslParseError, DslTypeError
from invari_spec.semantic_dsl.model import (
    ActionDecl,
    BoolType,
    CallExpr,
    CollectionType,
    CompletionRequiresDecl,
    EntityDecl,
    EnumType,
    Expr,
    FieldRef,
    ForbiddenDecl,
    InvariantDecl,
    IntType,
    LiteralExpr,
    NamedType,
    ObligationDecl,
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


_EXPR_OPS = {
    "Eq",
    "And",
    "Or",
    "Not",
    "Implies",
    "Add",
    "Sub",
    "Lt",
    "Le",
    "Gt",
    "Ge",
    "Contains",
    "Count",
    "CountUnique",
    "Changed",
    "Unchanged",
    "AddItem",
    "RemoveItem",
}

_EXPLORATION_BRANCH_NAME_PATTERNS = (
    re.compile(r".*_succeeds$"),
    re.compile(r".*_fails$"),
    re.compile(r".*_success$"),
    re.compile(r".*_failure$"),
    re.compile(r"should_.*"),
    re.compile(r"will_.*"),
    re.compile(r"can_.*"),
    re.compile(r".*_allowed$"),
    re.compile(r".*_retry$"),
    re.compile(r".*_valid$"),
)

_WORKFLOW_PROGRESS_STATUSES = {"CREATED", "PAID", "FAILED", "SHIPPED", "CANCELLED", "FALLBACK", "SUCCEEDED"}


class _Builder:
    def __init__(self, source_name: str) -> None:
        self.source_name = source_name
        self.workflow_name: str | None = None
        self.entities: dict[str, EntityDecl] = {}
        self.vars: dict[str, VarDecl] = {}
        self.init_exprs: tuple[Expr, ...] | None = None
        self.actions: list[ActionDecl] = []
        self.invariants: list[InvariantDecl] = []
        self.forbiddens: list[ForbiddenDecl] = []
        self.obligations: list[ObligationDecl] = []
        self.completion_requires: list[CompletionRequiresDecl] = []
        self.warnings: list[str] = []

    def parse_statement(self, stmt: ast.stmt) -> None:
        if not isinstance(stmt, ast.Expr) or not isinstance(stmt.value, ast.Call):
            raise self._parse_error(stmt, f"unsupported Python statement: {type(stmt).__name__}")
        call = stmt.value
        name = self._call_name(call)
        if name == "workflow":
            self._parse_workflow(call)
        elif name == "entity":
            self._parse_entity(call)
        elif name == "var":
            self._parse_var(call)
        elif name == "init":
            self._parse_init(call)
        elif name == "action":
            self._parse_action(call)
        elif name == "invariant":
            self._parse_invariant(call)
        elif name == "forbidden":
            self._parse_forbidden(call)
        elif name == "obligation":
            self._parse_obligation(call)
        elif name == "completion_requires":
            self._parse_completion_requires(call)
        else:
            raise self._parse_error(call, f"unsupported top-level DSL call: {name}")

    def build(self) -> WorkflowModel:
        if self.workflow_name is None:
            raise DslTypeError(f"{self.source_name}: missing workflow(...) declaration")
        if self.init_exprs is None:
            raise DslTypeError(f"{self.source_name}: missing init(...) declaration")
        if not self.actions:
            raise DslTypeError(f"{self.source_name}: at least one action(...) is required")
        self._validate_unique_names()
        self._validate_refs()
        self._validate_expr_shapes()
        self._validate_all_state_initialized()
        self._validate_no_changed_in_invariants()
        self._validate_action_updates()
        self._collect_exploration_warnings()
        return WorkflowModel(
            name=self.workflow_name,
            entities=dict(self.entities),
            vars=dict(self.vars),
            init=self.init_exprs,
            actions=tuple(self.actions),
            invariants=tuple(self.invariants),
            forbiddens=tuple(self.forbiddens),
            obligations=tuple(self.obligations),
            completion_requires=tuple(self.completion_requires),
            warnings=tuple(self.warnings),
        )

    def _parse_workflow(self, call: ast.Call) -> None:
        self._no_keywords(call)
        if self.workflow_name is not None:
            raise self._parse_error(call, "workflow(...) may only be declared once")
        if len(call.args) != 1:
            raise self._parse_error(call, "workflow(...) expects one string argument")
        self.workflow_name = self._string_arg(call.args[0], "workflow name")

    def _parse_entity(self, call: ast.Call) -> None:
        self._no_keywords(call)
        if len(call.args) != 2:
            raise self._parse_error(call, "entity(...) expects name and Record(...)")
        name = self._string_arg(call.args[0], "entity name")
        if name in self.entities or name in self.vars:
            raise self._parse_error(call, f"duplicate state declaration: {name}")
        record_call = self._expect_call(call.args[1], "Record")
        if record_call.args:
            raise self._parse_error(record_call, "Record(...) only accepts keyword fields")
        fields: dict[str, TypeRef] = {}
        for kw in record_call.keywords:
            if kw.arg is None:
                raise self._parse_error(record_call, "Record(...) does not accept **kwargs")
            if kw.arg in fields:
                raise self._parse_error(record_call, f"duplicate field {name}.{kw.arg}")
            fields[kw.arg] = self._parse_type(kw.value)
        if not fields:
            raise self._parse_error(record_call, "Record(...) must declare at least one field")
        self.entities[name] = EntityDecl(name=name, fields=fields)

    def _parse_var(self, call: ast.Call) -> None:
        self._no_keywords(call)
        if len(call.args) != 2:
            raise self._parse_error(call, "var(...) expects name and type")
        name = self._string_arg(call.args[0], "var name")
        if name in self.vars or name in self.entities:
            raise self._parse_error(call, f"duplicate state declaration: {name}")
        self.vars[name] = VarDecl(name=name, type_ref=self._parse_type(call.args[1]))

    def _parse_init(self, call: ast.Call) -> None:
        self._no_keywords(call)
        if self.init_exprs is not None:
            raise self._parse_error(call, "init(...) may only be declared once")
        self.init_exprs = tuple(self._parse_expr(arg) for arg in call.args)

    def _parse_action(self, call: ast.Call) -> None:
        if len(call.args) != 1:
            raise self._parse_error(call, "action(...) expects one name argument")
        name = self._string_arg(call.args[0], "action name")
        kwargs = self._kwargs(call)
        requires = self._expr_list(kwargs.pop("requires", None), "requires")
        changes = self._update_list(kwargs.pop("changes", None), "changes")
        emits = self._expr_list(kwargs.pop("emits", None), "emits")
        ensures = self._expr_list(kwargs.pop("ensures", None), "ensures")
        if kwargs:
            raise self._parse_error(call, f"unsupported action keyword(s): {', '.join(sorted(kwargs))}")
        self.actions.append(ActionDecl(name=name, requires=requires, changes=changes, emits=emits, ensures=ensures))

    def _parse_invariant(self, call: ast.Call) -> None:
        self._no_keywords(call)
        if len(call.args) != 2:
            raise self._parse_error(call, "invariant(...) expects name and predicate")
        self.invariants.append(
            InvariantDecl(
                name=self._string_arg(call.args[0], "invariant name"),
                predicate=self._parse_expr(call.args[1]),
            )
        )

    def _parse_forbidden(self, call: ast.Call) -> None:
        if len(call.args) != 1:
            raise self._parse_error(call, "forbidden(...) expects one name argument")
        kwargs = self._kwargs(call)
        when = kwargs.pop("when", None)
        if when is None:
            raise self._parse_error(call, "forbidden(...) requires when=...")
        if kwargs:
            raise self._parse_error(call, f"unsupported forbidden keyword(s): {', '.join(sorted(kwargs))}")
        self.forbiddens.append(
            ForbiddenDecl(
                name=self._string_arg(call.args[0], "forbidden name"),
                predicate=self._parse_expr(when),
            )
        )

    def _parse_obligation(self, call: ast.Call) -> None:
        if len(call.args) != 1:
            raise self._parse_error(call, "obligation(...) expects one name argument")
        kwargs = self._kwargs(call)
        trigger = kwargs.pop("trigger", None)
        eventually = kwargs.pop("must_eventually", None)
        if trigger is None or eventually is None:
            raise self._parse_error(call, "obligation(...) requires trigger=... and must_eventually=...")
        if kwargs:
            raise self._parse_error(call, f"unsupported obligation keyword(s): {', '.join(sorted(kwargs))}")
        self.obligations.append(
            ObligationDecl(
                name=self._string_arg(call.args[0], "obligation name"),
                trigger=self._parse_expr(trigger),
                must_eventually=self._parse_expr(eventually),
            )
        )

    def _parse_completion_requires(self, call: ast.Call) -> None:
        kwargs = self._kwargs(call)
        if len(call.args) > 1:
            raise self._parse_error(call, "completion_requires(...) accepts at most one name argument")
        name = (
            self._string_arg(call.args[0], "completion requirement name")
            if call.args
            else f"completion_requires_{len(self.completion_requires) + 1}"
        )
        outcome = kwargs.pop("outcome", None)
        condition = kwargs.pop("condition", None)
        if outcome is None or condition is None:
            raise self._parse_error(call, "completion_requires(...) requires outcome=... and condition=...")
        if kwargs:
            raise self._parse_error(call, f"unsupported completion_requires keyword(s): {', '.join(sorted(kwargs))}")
        self.completion_requires.append(
            CompletionRequiresDecl(
                name=name,
                outcome=self._parse_expr(outcome),
                condition=self._parse_expr(condition),
            )
        )

    def _parse_type(self, node: ast.AST) -> TypeRef:
        if isinstance(node, ast.Name):
            if node.id == "Bool":
                return BoolType()
            if node.id == "Int":
                return IntType()
            return NamedType(node.id)
        if isinstance(node, ast.Call):
            name = self._call_name(node)
            if name == "Enum":
                self._no_keywords(node)
                values = tuple(self._string_arg(arg, "enum value") for arg in node.args)
                if not values:
                    raise self._parse_error(node, "Enum(...) must contain at least one value")
                if len(set(values)) != len(values):
                    raise self._parse_error(node, "Enum(...) contains duplicate values")
                return EnumType(values=values)
            if name == "Collection":
                self._no_keywords(node)
                if len(node.args) != 1:
                    raise self._parse_error(node, "Collection(...) expects one item type")
                return CollectionType(item_type=self._parse_type(node.args[0]))
        raise self._parse_error(node, f"unsupported type expression: {ast.dump(node, include_attributes=False)}")

    def _parse_expr(self, node: ast.AST) -> Expr:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (bool, int, str)):
                return LiteralExpr(node.value)
            raise self._parse_error(node, f"unsupported literal: {node.value!r}")
        if isinstance(node, ast.Name):
            if node.id in {"True", "False"}:
                return LiteralExpr(node.id == "True")
            raise self._parse_error(node, f"bare name is not a DSL expression: {node.id}")
        if not isinstance(node, ast.Call):
            raise self._parse_error(node, f"unsupported expression: {type(node).__name__}")
        name = self._call_name(node)
        if name == "Var":
            self._no_keywords(node)
            if len(node.args) != 1:
                raise self._parse_error(node, "Var(...) expects one name")
            return RefExpr(VarRef(self._string_arg(node.args[0], "var reference")))
        if name == "Field":
            self._no_keywords(node)
            if len(node.args) != 2:
                raise self._parse_error(node, "Field(...) expects entity and field")
            return RefExpr(
                FieldRef(
                    entity=self._string_arg(node.args[0], "field entity"),
                    field=self._string_arg(node.args[1], "field name"),
                )
            )
        if name not in _EXPR_OPS:
            raise self._parse_error(node, f"unsupported DSL expression call: {name}")
        self._no_keywords(node)
        return CallExpr(name, tuple(self._parse_expr(arg) for arg in node.args))

    def _parse_update(self, node: ast.AST) -> Update:
        call = self._expect_call_any(node)
        name = self._call_name(call)
        if name == "Set":
            self._no_keywords(call)
            if len(call.args) != 2:
                raise self._parse_error(call, "Set(...) expects var name and value")
            return SetUpdate(VarRef(self._string_arg(call.args[0], "Set target")), self._parse_expr(call.args[1]))
        if name == "SetField":
            self._no_keywords(call)
            if len(call.args) != 3:
                raise self._parse_error(call, "SetField(...) expects entity, field, and value")
            return SetFieldUpdate(
                FieldRef(
                    entity=self._string_arg(call.args[0], "SetField entity"),
                    field=self._string_arg(call.args[1], "SetField field"),
                ),
                self._parse_expr(call.args[2]),
            )
        if name == "Unchanged":
            self._no_keywords(call)
            if len(call.args) != 1:
                raise self._parse_error(call, "Unchanged(...) expects one ref")
            expr = self._parse_expr(call.args[0])
            if not isinstance(expr, RefExpr):
                raise self._parse_error(call, "Unchanged(...) target must be Var(...) or Field(...)")
            return UnchangedUpdate(expr.ref)
        raise self._parse_error(call, f"unsupported update call: {name}")

    def _expr_list(self, node: ast.AST | None, label: str) -> tuple[Expr, ...]:
        if node is None:
            return tuple()
        if not isinstance(node, ast.List):
            raise self._parse_error(node, f"{label}= must be a list")
        return tuple(self._parse_expr(item) for item in node.elts)

    def _update_list(self, node: ast.AST | None, label: str) -> tuple[Update, ...]:
        if node is None:
            return tuple()
        if not isinstance(node, ast.List):
            raise self._parse_error(node, f"{label}= must be a list")
        return tuple(self._parse_update(item) for item in node.elts)

    def _validate_unique_names(self) -> None:
        self._ensure_unique([action.name for action in self.actions], "action")
        self._ensure_unique([inv.name for inv in self.invariants], "invariant")
        self._ensure_unique([f.name for f in self.forbiddens], "forbidden")
        self._ensure_unique([o.name for o in self.obligations], "obligation")
        self._ensure_unique([c.name for c in self.completion_requires], "completion requirement")

    def _validate_refs(self) -> None:
        for expr in self._all_exprs():
            for ref in self._refs_in_expr(expr):
                self._validate_ref(ref)
        for action in self.actions:
            for update in action.changes:
                if isinstance(update, SetUpdate):
                    self._validate_ref(update.target)
                    for ref in self._refs_in_expr(update.value):
                        self._validate_ref(ref)
                elif isinstance(update, SetFieldUpdate):
                    self._validate_ref(update.target)
                    for ref in self._refs_in_expr(update.value):
                        self._validate_ref(ref)
                elif isinstance(update, UnchangedUpdate):
                    self._validate_ref(update.target)

    def _validate_expr_shapes(self) -> None:
        for expr in self._all_exprs():
            self._validate_expr_shape(expr)
        for action in self.actions:
            for update in action.changes:
                if isinstance(update, (SetUpdate, SetFieldUpdate)):
                    self._validate_expr_shape(update.value)

    def _validate_expr_shape(self, expr: Expr) -> None:
        if not isinstance(expr, CallExpr):
            return
        expected: dict[str, int] = {
            "Eq": 2,
            "Not": 1,
            "Implies": 2,
            "Add": 2,
            "Sub": 2,
            "Lt": 2,
            "Le": 2,
            "Gt": 2,
            "Ge": 2,
            "Contains": 2,
            "Count": 1,
            "CountUnique": 1,
            "Changed": 1,
            "Unchanged": 1,
            "AddItem": 2,
            "RemoveItem": 2,
        }
        if expr.op in expected and len(expr.args) != expected[expr.op]:
            raise DslTypeError(
                f"{self.source_name}: {expr.op}(...) expects {expected[expr.op]} argument(s), got {len(expr.args)}"
            )
        if expr.op in {"And", "Or"} and not expr.args:
            raise DslTypeError(f"{self.source_name}: {expr.op}(...) expects at least one argument")
        if expr.op in {"Changed", "Unchanged"} and not isinstance(expr.args[0], RefExpr):
            raise DslTypeError(f"{self.source_name}: {expr.op}(...) expects Var(...) or Field(...)")
        for arg in expr.args:
            self._validate_expr_shape(arg)

    def _validate_all_state_initialized(self) -> None:
        initialized = {self._ref_key(ref) for expr in self.init_exprs or tuple() for ref in self._eq_lhs_refs(expr)}
        missing: list[str] = []
        for entity in self.entities.values():
            for field in entity.fields:
                key = f"{entity.name}.{field}"
                if key not in initialized:
                    missing.append(key)
        for name in self.vars:
            if name not in initialized:
                missing.append(name)
        if missing:
            raise DslTypeError(f"{self.source_name}: missing init(...) values for: {', '.join(sorted(missing))}")

    def _validate_no_changed_in_invariants(self) -> None:
        for inv in self.invariants:
            if self._contains_op(inv.predicate, {"Changed", "Unchanged"}):
                raise DslTypeError(
                    f"{self.source_name}: invariant {inv.name!r} cannot use Changed(...) or Unchanged(...)"
                )

    def _validate_action_updates(self) -> None:
        for action in self.actions:
            seen: set[str] = set()
            for update in action.changes:
                target = self._update_target_key(update)
                if target in seen:
                    raise DslTypeError(f"{self.source_name}: action {action.name!r} updates {target} more than once")
                seen.add(target)

    def _collect_exploration_warnings(self) -> None:
        self._warn_on_frozen_outcome_variables()
        self._warn_on_read_before_create()
        self._warn_on_existence_state_inconsistency()
        self._warn_on_dead_branches_from_frozen_outcomes()

    def _collect_init_assigned_refs(self) -> dict[str, Expr]:
        assigned: dict[str, Expr] = {}
        for expr in self.init_exprs or tuple():
            extracted = self._eq_assignment(expr)
            if extracted is None:
                continue
            ref, value = extracted
            assigned[self._ref_key(ref)] = value
        return assigned

    def _collect_guard_reads(self) -> dict[str, set[str]]:
        reads: dict[str, set[str]] = {}
        for action in self.actions:
            refs: set[str] = set()
            for expr in action.requires:
                refs.update(self._ref_key(ref) for ref in self._refs_in_expr(expr))
            reads[action.name] = refs
        return reads

    def _collect_changed_refs(self) -> set[str]:
        changed: set[str] = set()
        for action in self.actions:
            for update in action.changes:
                changed.add(self._update_target_key(update))
        return changed

    def _entity_existence_vars(self) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for entity_name in self.entities:
            candidate = f"{entity_name}_exists"
            if candidate in self.vars:
                mapping[entity_name] = candidate
        return mapping

    def _warn_on_frozen_outcome_variables(self) -> None:
        init_assigned = self._collect_init_assigned_refs()
        guard_reads = self._collect_guard_reads()
        changed = self._collect_changed_refs()
        read_refs = {ref for refs in guard_reads.values() for ref in refs}
        for ref_key in sorted(read_refs):
            if ref_key not in init_assigned or ref_key in changed:
                continue
            if not self._looks_like_branch_selector(ref_key):
                continue
            self.warnings.append(
                f"W_EXPLORATION_FROZEN_OUTCOME: {ref_key} is initialized in init, used in action guards, and never changed; "
                "this may freeze one future branch and make success or failure states unreachable"
            )

    def _warn_on_read_before_create(self) -> None:
        existence_vars = self._entity_existence_vars()
        for action in self.actions:
            negated_entities = {
                entity_name
                for entity_name, exists_var in existence_vars.items()
                if any(self._expr_negates_var(expr, exists_var) for expr in action.requires)
            }
            if not negated_entities:
                continue
            guard_refs: list[Ref] = []
            for expr in action.requires:
                guard_refs.extend(self._refs_in_expr(expr))
            for entity_name in sorted(negated_entities):
                for ref in guard_refs:
                    if isinstance(ref, FieldRef) and ref.entity == entity_name:
                        self.warnings.append(
                            f"W_EXPLORATION_READ_BEFORE_CREATE: action {action.name} reads {ref.entity}.{ref.field} while "
                            f"{entity_name}_exists is false in its guard; construct the entity state in changes instead "
                            "of gating on placeholder fields"
                        )
                        break

    def _warn_on_existence_state_inconsistency(self) -> None:
        init_assigned = self._collect_init_assigned_refs()
        for entity_name, exists_var in self._entity_existence_vars().items():
            exists_value = init_assigned.get(exists_var)
            if not self._is_false_literal(exists_value):
                continue
            for field_name, value in self._entity_init_fields(entity_name, init_assigned).items():
                literal = self._literal_string(value)
                if literal and literal.upper() in _WORKFLOW_PROGRESS_STATUSES:
                    self.warnings.append(
                        f"W_EXPLORATION_EXISTENCE_STATE_INCONSISTENCY: {exists_var} is initialized to FALSE while "
                        f"{entity_name}.{field_name} is initialized to workflow status {literal!r}; use a canonical "
                        "start state or treat the entity as existing"
                    )
                    break

    def _warn_on_dead_branches_from_frozen_outcomes(self) -> None:
        init_assigned = self._collect_init_assigned_refs()
        changed = self._collect_changed_refs()
        for ref_key, value in sorted(init_assigned.items()):
            if ref_key in changed or not self._looks_like_branch_selector(ref_key):
                continue
            branches = []
            for action in self.actions:
                branch = self._branch_guard_for_ref(action.requires, ref_key)
                if branch is not None:
                    branches.append((action.name, branch))
            if any(branch for _, branch in branches) and any(not branch for _, branch in branches):
                fixed_value = self._literal_bool(value)
                if fixed_value is None:
                    continue
                unreachable = [name for name, branch in branches if branch != fixed_value]
                if unreachable:
                    self.warnings.append(
                        f"W_EXPLORATION_DEAD_BRANCH: {ref_key} is frozen by init and never changed; guarded branch action(s) "
                        f"{', '.join(sorted(unreachable))} may be permanently unreachable"
                    )

    def _eq_assignment(self, expr: Expr) -> tuple[Ref, Expr] | None:
        if isinstance(expr, CallExpr) and expr.op == "Eq" and len(expr.args) == 2:
            lhs, rhs = expr.args
            if isinstance(lhs, RefExpr):
                return lhs.ref, rhs
        return None

    def _entity_init_fields(self, entity_name: str, init_assigned: dict[str, Expr]) -> dict[str, Expr]:
        prefix = f"{entity_name}."
        return {key[len(prefix):]: value for key, value in init_assigned.items() if key.startswith(prefix)}

    def _looks_like_branch_selector(self, ref_key: str) -> bool:
        leaf = ref_key.rsplit(".", 1)[-1]
        return any(pattern.fullmatch(leaf) for pattern in _EXPLORATION_BRANCH_NAME_PATTERNS)

    def _expr_negates_var(self, expr: Expr, var_name: str) -> bool:
        if not isinstance(expr, CallExpr) or expr.op != "Not" or len(expr.args) != 1:
            return False
        target = expr.args[0]
        return isinstance(target, RefExpr) and isinstance(target.ref, VarRef) and target.ref.name == var_name

    def _branch_guard_for_ref(self, exprs: tuple[Expr, ...], ref_key: str) -> bool | None:
        for expr in exprs:
            if isinstance(expr, RefExpr) and self._ref_key(expr.ref) == ref_key:
                return True
            if (
                isinstance(expr, CallExpr)
                and expr.op == "Not"
                and len(expr.args) == 1
                and isinstance(expr.args[0], RefExpr)
                and self._ref_key(expr.args[0].ref) == ref_key
            ):
                return False
            if (
                isinstance(expr, CallExpr)
                and expr.op == "Eq"
                and len(expr.args) == 2
                and isinstance(expr.args[0], RefExpr)
                and self._ref_key(expr.args[0].ref) == ref_key
            ):
                bool_value = self._literal_bool(expr.args[1])
                if bool_value is not None:
                    return bool_value
        return None

    def _is_false_literal(self, expr: Expr | None) -> bool:
        return isinstance(expr, LiteralExpr) and expr.value is False

    def _literal_bool(self, expr: Expr) -> bool | None:
        if isinstance(expr, LiteralExpr) and isinstance(expr.value, bool):
            return expr.value
        return None

    def _literal_string(self, expr: Expr) -> str | None:
        if isinstance(expr, LiteralExpr) and isinstance(expr.value, str):
            return expr.value
        return None

    def _all_exprs(self) -> list[Expr]:
        exprs: list[Expr] = []
        exprs.extend(self.init_exprs or tuple())
        for action in self.actions:
            exprs.extend(action.requires)
            exprs.extend(action.emits)
            exprs.extend(action.ensures)
        exprs.extend(inv.predicate for inv in self.invariants)
        exprs.extend(f.predicate for f in self.forbiddens)
        for obligation in self.obligations:
            exprs.append(obligation.trigger)
            exprs.append(obligation.must_eventually)
        for completion in self.completion_requires:
            exprs.append(completion.outcome)
            exprs.append(completion.condition)
        return exprs

    def _refs_in_expr(self, expr: Expr) -> list[Ref]:
        if isinstance(expr, RefExpr):
            return [expr.ref]
        if isinstance(expr, CallExpr):
            refs: list[Ref] = []
            for arg in expr.args:
                refs.extend(self._refs_in_expr(arg))
            return refs
        return []

    def _eq_lhs_refs(self, expr: Expr) -> list[Ref]:
        if isinstance(expr, CallExpr) and expr.op == "Eq" and expr.args:
            lhs = expr.args[0]
            if isinstance(lhs, RefExpr):
                return [lhs.ref]
        return []

    def _contains_op(self, expr: Expr, ops: set[str]) -> bool:
        if isinstance(expr, CallExpr):
            return expr.op in ops or any(self._contains_op(arg, ops) for arg in expr.args)
        return False

    def _validate_ref(self, ref: Ref) -> None:
        if isinstance(ref, VarRef):
            if ref.name not in self.vars:
                raise DslTypeError(f"{self.source_name}: unknown variable {ref.name}")
        elif isinstance(ref, FieldRef):
            entity = self.entities.get(ref.entity)
            if entity is None:
                raise DslTypeError(f"{self.source_name}: unknown entity {ref.entity}")
            if ref.field not in entity.fields:
                raise DslTypeError(f"{self.source_name}: unknown field {ref.entity}.{ref.field}")

    def _ref_key(self, ref: Ref) -> str:
        if isinstance(ref, VarRef):
            return ref.name
        return f"{ref.entity}.{ref.field}"

    def _update_target_key(self, update: Update) -> str:
        if isinstance(update, SetUpdate):
            return self._ref_key(update.target)
        if isinstance(update, SetFieldUpdate):
            return self._ref_key(update.target)
        return self._ref_key(update.target)

    def _ensure_unique(self, values: list[str], label: str) -> None:
        seen: set[str] = set()
        for value in values:
            if value in seen:
                raise DslTypeError(f"{self.source_name}: duplicate {label} name {value!r}")
            seen.add(value)

    def _kwargs(self, call: ast.Call) -> dict[str, ast.AST]:
        kwargs: dict[str, ast.AST] = {}
        for kw in call.keywords:
            if kw.arg is None:
                raise self._parse_error(call, "**kwargs are not supported")
            if kw.arg in kwargs:
                raise self._parse_error(call, f"duplicate keyword {kw.arg}")
            kwargs[kw.arg] = kw.value
        return kwargs

    def _no_keywords(self, call: ast.Call) -> None:
        if call.keywords:
            raise self._parse_error(call, f"{self._call_name(call)}(...) does not accept keyword arguments")

    def _call_name(self, call: ast.Call) -> str:
        if not isinstance(call.func, ast.Name):
            raise self._parse_error(call, "only direct DSL constructor calls are supported")
        return call.func.id

    def _expect_call(self, node: ast.AST, name: str) -> ast.Call:
        call = self._expect_call_any(node)
        actual = self._call_name(call)
        if actual != name:
            raise self._parse_error(node, f"expected {name}(...), got {actual}(...)")
        return call

    def _expect_call_any(self, node: ast.AST) -> ast.Call:
        if not isinstance(node, ast.Call):
            raise self._parse_error(node, "expected DSL call")
        return node

    def _string_arg(self, node: ast.AST, label: str) -> str:
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            raise self._parse_error(node, f"{label} must be a string literal")
        if not node.value:
            raise self._parse_error(node, f"{label} must not be empty")
        return node.value

    def _parse_error(self, node: ast.AST, message: str) -> DslParseError:
        line = getattr(node, "lineno", None)
        prefix = f"{self.source_name}:{line}: " if line is not None else f"{self.source_name}: "
        return DslParseError(prefix + message)


def parse_dsl_source(source: str, *, source_name: str = "<dsl>") -> WorkflowModel:
    try:
        module = ast.parse(source, filename=source_name)
    except SyntaxError as exc:
        line = exc.lineno or "?"
        raise DslParseError(f"{source_name}:{line}: syntax error: {exc.msg}") from exc
    builder = _Builder(source_name)
    for stmt in module.body:
        builder.parse_statement(stmt)
    return builder.build()


def parse_dsl_file(path: Path | str) -> WorkflowModel:
    source_path = Path(path)
    return parse_dsl_source(source_path.read_text(encoding="utf-8"), source_name=str(source_path))
