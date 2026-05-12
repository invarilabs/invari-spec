from __future__ import annotations

import unittest

from invari_spec.semantic_dsl import parse_dsl_source
from invari_spec.semantic_dsl.errors import DslParseError, DslTypeError
from invari_spec.semantic_dsl.model import EnumType


REVIEW_WORKFLOW = '''
workflow("generic_review_workflow")

entity("request", Record(
    status=Enum("draft", "submitted", "approved", "rejected"),
    requires_review=Bool,
    review_complete=Bool,
))

entity("actor", Record(
    role=Enum("author", "reviewer", "manager"),
))

init(
    Eq(Field("request", "status"), "draft"),
    Eq(Field("request", "requires_review"), True),
    Eq(Field("request", "review_complete"), False),
    Eq(Field("actor", "role"), "author"),
)

action(
    "submit_request",
    requires=[
        Eq(Field("request", "status"), "draft"),
    ],
    changes=[
        SetField("request", "status", "submitted"),
    ],
)

action(
    "complete_review",
    requires=[
        Eq(Field("request", "status"), "submitted"),
        Eq(Field("actor", "role"), "reviewer"),
    ],
    changes=[
        SetField("request", "review_complete", True),
    ],
)

action(
    "approve_request",
    requires=[
        Eq(Field("request", "status"), "submitted"),
        Implies(
            Field("request", "requires_review"),
            Field("request", "review_complete"),
        ),
    ],
    changes=[
        SetField("request", "status", "approved"),
    ],
)

invariant(
    "approved_requests_requiring_review_have_completed_review",
    Implies(
        And(
            Eq(Field("request", "status"), "approved"),
            Field("request", "requires_review"),
        ),
        Field("request", "review_complete"),
    ),
)

forbidden(
    "non_reviewer_completes_review",
    when=And(
        Changed(Field("request", "review_complete")),
        Not(Eq(Field("actor", "role"), "reviewer")),
    ),
)

obligation(
    "submitted_requests_eventually_reviewed",
    trigger=Eq(Field("request", "status"), "submitted"),
    must_eventually=Field("request", "review_complete"),
)
'''


class SemanticDslParserTest(unittest.TestCase):
    def test_parse_review_workflow_model(self) -> None:
        model = parse_dsl_source(REVIEW_WORKFLOW)

        self.assertEqual(model.name, "generic_review_workflow")
        self.assertEqual(set(model.entities), {"request", "actor"})
        self.assertEqual(set(model.entities["request"].fields), {"status", "requires_review", "review_complete"})
        status_type = model.entities["request"].fields["status"]
        self.assertIsInstance(status_type, EnumType)
        self.assertEqual(status_type.values, ("draft", "submitted", "approved", "rejected"))
        self.assertEqual(len(model.actions), 3)
        self.assertEqual(len(model.invariants), 1)
        self.assertEqual(len(model.forbiddens), 1)
        self.assertEqual(len(model.obligations), 1)

    def test_rejects_arbitrary_python(self) -> None:
        with self.assertRaises(DslParseError):
            parse_dsl_source("import os\nworkflow('x')\n")

    def test_rejects_unknown_field(self) -> None:
        source = REVIEW_WORKFLOW.replace('Field("request", "status")', 'Field("request", "missing")', 1)
        with self.assertRaises(DslTypeError):
            parse_dsl_source(source)

    def test_rejects_duplicate_action_names(self) -> None:
        source = REVIEW_WORKFLOW.replace('"complete_review"', '"submit_request"', 1)
        with self.assertRaises(DslTypeError):
            parse_dsl_source(source)

    def test_rejects_changed_in_invariant(self) -> None:
        source = REVIEW_WORKFLOW.replace(
            "Implies(\n        And(\n            Eq(Field(\"request\", \"status\"), \"approved\"),\n            Field(\"request\", \"requires_review\"),\n        ),\n        Field(\"request\", \"review_complete\"),\n    )",
            'Changed(Field("request", "review_complete"))',
        )
        with self.assertRaises(DslTypeError):
            parse_dsl_source(source)

    def test_requires_all_state_initialized(self) -> None:
        source = REVIEW_WORKFLOW.replace('    Eq(Field("actor", "role"), "author"),\n', "")
        with self.assertRaises(DslTypeError):
            parse_dsl_source(source)

    def test_rejects_invalid_expression_arity_during_validation(self) -> None:
        source = REVIEW_WORKFLOW.replace(
            'Eq(Field("request", "status"), "draft")',
            'Eq(Field("request", "status"), "draft", "extra")',
            1,
        )
        with self.assertRaisesRegex(DslTypeError, r"Eq\(\.\.\.\) expects 2 argument"):
            parse_dsl_source(source)

    def test_rejects_changed_without_ref_during_validation(self) -> None:
        source = REVIEW_WORKFLOW.replace(
            'Changed(Field("request", "review_complete"))',
            'Changed(Eq(Field("request", "review_complete"), True))',
            1,
        )
        with self.assertRaisesRegex(DslTypeError, r"Changed\(\.\.\.\) expects Var\(\.\.\.\) or Field\(\.\.\.\)"):
            parse_dsl_source(source)

    def test_warns_on_frozen_outcome_variables(self) -> None:
        source = '''
workflow("frozen_outcome")

entity("payment", Record(
    status=Enum("pending", "success", "failed"),
    payment_succeeds=Bool,
))

init(
    Eq(Field("payment", "status"), "pending"),
    Eq(Field("payment", "payment_succeeds"), False),
)

action(
    "payment_attempt_succeeds",
    requires=[
        Eq(Field("payment", "status"), "pending"),
        Field("payment", "payment_succeeds"),
    ],
    changes=[
        SetField("payment", "status", "success"),
    ],
)

action(
    "payment_attempt_fails",
    requires=[
        Eq(Field("payment", "status"), "pending"),
        Not(Field("payment", "payment_succeeds")),
    ],
    changes=[
        SetField("payment", "status", "failed"),
    ],
)
'''
        model = parse_dsl_source(source)
        self.assertTrue(any(w.startswith("W_EXPLORATION_FROZEN_OUTCOME: payment.payment_succeeds") for w in model.warnings))
        self.assertTrue(any(w.startswith("W_EXPLORATION_DEAD_BRANCH: payment.payment_succeeds") for w in model.warnings))

    def test_warns_on_read_before_create(self) -> None:
        source = '''
workflow("read_before_create")

entity("order", Record(
    status=Enum("created", "failed"),
    amount=Int,
))

var("order_exists", Bool)

init(
    Eq(Field("order", "status"), "created"),
    Eq(Field("order", "amount"), 0),
    Eq(Var("order_exists"), False),
)

action(
    "create_order",
    requires=[
        Not(Var("order_exists")),
        Gt(Field("order", "amount"), 0),
    ],
    changes=[
        Set("order_exists", True),
    ],
)
'''
        model = parse_dsl_source(source)
        self.assertTrue(any(w.startswith("W_EXPLORATION_READ_BEFORE_CREATE: action create_order reads order.amount") for w in model.warnings))

    def test_warns_on_existence_state_inconsistency(self) -> None:
        source = '''
workflow("existence_inconsistency")

entity("order", Record(
    status=Enum("CREATED", "FAILED"),
))

var("order_exists", Bool)

init(
    Eq(Field("order", "status"), "CREATED"),
    Eq(Var("order_exists"), False),
)

action(
    "noop",
    requires=[
        Not(Var("order_exists")),
    ],
    changes=[
        Set("order_exists", True),
    ],
)
'''
        model = parse_dsl_source(source)
        self.assertTrue(any(w.startswith("W_EXPLORATION_EXISTENCE_STATE_INCONSISTENCY: order_exists") for w in model.warnings))

    def test_valid_branching_model_emits_no_exploration_warnings(self) -> None:
        source = '''
workflow("valid_branching")

entity("payment", Record(
    status=Enum("pending", "success", "failed"),
))

init(
    Eq(Field("payment", "status"), "pending"),
)

action(
    "payment_attempt_succeeds",
    requires=[
        Eq(Field("payment", "status"), "pending"),
    ],
    changes=[
        SetField("payment", "status", "success"),
    ],
)

action(
    "payment_attempt_fails",
    requires=[
        Eq(Field("payment", "status"), "pending"),
    ],
    changes=[
        SetField("payment", "status", "failed"),
    ],
)
'''
        model = parse_dsl_source(source)
        self.assertEqual(model.warnings, ())

    def test_parses_string_action_emits(self) -> None:
        source = '''
workflow("events")

entity("refund", Record(
    status=Enum("requested", "refunded"),
))

init(
    Eq(Field("refund", "status"), "requested"),
)

action(
    "process_refund",
    changes=[
        SetField("refund", "status", "refunded"),
    ],
    emits=[
        "refund_payment_issued",
    ],
)

obligation(
    "payment_was_issued",
    trigger=Eq(Field("refund", "status"), "refunded"),
    must_eventually=SeenEvent("refund_payment_issued"),
)
'''
        model = parse_dsl_source(source)

        self.assertEqual(model.actions[0].emits, ("refund_payment_issued",))

    def test_rejects_non_string_action_emits(self) -> None:
        source = '''
workflow("events")

entity("refund", Record(
    status=Enum("requested", "refunded"),
))

init(
    Eq(Field("refund", "status"), "requested"),
)

action(
    "process_refund",
    changes=[
        SetField("refund", "status", "refunded"),
    ],
    emits=[
        Field("refund", "status"),
    ],
)
'''
        with self.assertRaises(DslParseError):
            parse_dsl_source(source)

    def test_rejects_unknown_event_predicate(self) -> None:
        source = '''
workflow("events")

entity("refund", Record(
    status=Enum("requested", "refunded"),
))

init(
    Eq(Field("refund", "status"), "requested"),
)

action(
    "process_refund",
    changes=[
        SetField("refund", "status", "refunded"),
    ],
)

obligation(
    "payment_was_issued",
    trigger=Eq(Field("refund", "status"), "refunded"),
    must_eventually=SeenEvent("refund_payment_issued"),
)
'''
        with self.assertRaisesRegex(DslTypeError, "never emitted"):
            parse_dsl_source(source)

    def test_rejects_too_many_checked_events(self) -> None:
        event_values = ", ".join(f'"event_{idx}"' for idx in range(11))
        must_eventually = "Or(" + ", ".join(f'SeenEvent("event_{idx}")' for idx in range(11)) + ")"
        source = f'''
workflow("many_events")

entity("task", Record(
    status=Enum("ready", "done"),
))

init(
    Eq(Field("task", "status"), "ready"),
)

action(
    "finish",
    changes=[
        SetField("task", "status", "done"),
    ],
    emits=[
        {event_values},
    ],
)

obligation(
    "too_many_events",
    trigger=Eq(Field("task", "status"), "done"),
    must_eventually={must_eventually},
)
'''
        with self.assertRaisesRegex(DslTypeError, "MAX_LOWERED_EVENTS"):
            parse_dsl_source(source)


if __name__ == "__main__":
    unittest.main()
