from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class BoolType:
    pass


@dataclass(frozen=True)
class IntType:
    pass


@dataclass(frozen=True)
class EnumType:
    values: tuple[str, ...]


@dataclass(frozen=True)
class CollectionType:
    item_type: "TypeRef"


@dataclass(frozen=True)
class IntRangeType:
    lo: int
    hi: int


@dataclass(frozen=True)
class IntSetType:
    values: tuple[int, ...]


@dataclass(frozen=True)
class NamedType:
    name: str


TypeRef = Union[BoolType, IntType, IntRangeType, IntSetType, EnumType, CollectionType, NamedType]
FairnessKind = Literal["weak", "strong"]


@dataclass(frozen=True)
class EntityDecl:
    name: str
    fields: dict[str, TypeRef]


@dataclass(frozen=True)
class VarDecl:
    name: str
    type_ref: TypeRef


@dataclass(frozen=True)
class VarRef:
    name: str


@dataclass(frozen=True)
class FieldRef:
    entity: str
    field: str


Ref = Union[VarRef, FieldRef]


@dataclass(frozen=True)
class LiteralExpr:
    value: str | int | bool


@dataclass(frozen=True)
class RefExpr:
    ref: Ref


@dataclass(frozen=True)
class CallExpr:
    op: str
    args: tuple["Expr", ...]


@dataclass(frozen=True)
class AnyOfExpr:
    type_ref: TypeRef


Expr = Union[LiteralExpr, RefExpr, CallExpr, AnyOfExpr]


@dataclass(frozen=True)
class SetUpdate:
    target: VarRef
    value: Expr


@dataclass(frozen=True)
class SetFieldUpdate:
    target: FieldRef
    value: Expr


@dataclass(frozen=True)
class UnchangedUpdate:
    target: Ref


Update = Union[SetUpdate, SetFieldUpdate, UnchangedUpdate]


@dataclass(frozen=True)
class ActionDecl:
    name: str
    requires: tuple[Expr, ...]
    changes: tuple[Update, ...]
    emits: tuple[str, ...]
    ensures: tuple[Expr, ...]
    fairness: FairnessKind | None = None


@dataclass(frozen=True)
class InvariantDecl:
    name: str
    predicate: Expr


@dataclass(frozen=True)
class ForbiddenDecl:
    name: str
    predicate: Expr


@dataclass(frozen=True)
class ObligationDecl:
    name: str
    trigger: Expr
    must_eventually: Expr


@dataclass(frozen=True)
class CompletionRequiresDecl:
    name: str
    outcome: Expr
    condition: Expr


@dataclass(frozen=True)
class WorkflowModel:
    name: str
    entities: dict[str, EntityDecl]
    vars: dict[str, VarDecl]
    init: tuple[Expr, ...]
    actions: tuple[ActionDecl, ...]
    invariants: tuple[InvariantDecl, ...]
    forbiddens: tuple[ForbiddenDecl, ...]
    obligations: tuple[ObligationDecl, ...]
    completion_requires: tuple[CompletionRequiresDecl, ...]
    warnings: tuple[str, ...]
