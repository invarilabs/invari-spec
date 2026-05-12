from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from invari_spec.semantic_dsl import build_cfg, lower_to_tla, parse_dsl_source, tla_lowering_warnings
from test_semantic_dsl_parser import REVIEW_WORKFLOW


def _load_tla_sanity_module():
    mod_path = Path(__file__).resolve().parents[1] / "invari_spec" / "pipeline" / "tla_sanity.py"
    spec = importlib.util.spec_from_file_location("semantic_dsl_tla_sanity", mod_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SemanticDslTlaTest(unittest.TestCase):
    def _model(self):
        return parse_dsl_source(REVIEW_WORKFLOW)

    def test_lowers_entities_to_record_variables(self) -> None:
        tla = lower_to_tla(self._model())

        self.assertIn("VARIABLES request, actor", tla)
        self.assertIn("request.status", tla)
        self.assertIn('request\' = [request EXCEPT !.status = "submitted"]', tla)

    def test_lowers_init_entity_fields_to_record_values(self) -> None:
        tla = lower_to_tla(self._model())

        self.assertIn(
            'request = [status |-> "draft", requires_review |-> TRUE, review_complete |-> FALSE]',
            tla,
        )
        self.assertIn('actor = [role |-> "author"]', tla)
        init_block = tla.split("Init ==\n", 1)[1].split("\n\nSubmitRequest ==", 1)[0]
        self.assertNotIn('request.status = "draft"', init_block)
        self.assertNotIn('request.review_complete = FALSE', init_block)

    def test_lowers_next_and_spec(self) -> None:
        tla = lower_to_tla(self._model())

        self.assertIn("Next ==", tla)
        self.assertIn("\\/ SubmitRequest", tla)
        self.assertIn("\\/ CompleteReview", tla)
        self.assertIn("Spec ==", tla)
        self.assertIn("Init /\\ [][Next]_vars", tla)

    def test_lowers_invariant_to_cfg(self) -> None:
        cfg = build_cfg(self._model())

        self.assertIn("INVARIANTS", cfg)
        self.assertIn("TypeOk", cfg)
        self.assertIn("ApprovedRequestsRequiringReviewHaveCompletedReview", cfg)

    def test_transition_forbidden_lowers_to_property(self) -> None:
        model = self._model()
        tla = lower_to_tla(model)
        cfg = build_cfg(model)

        self.assertIn("NonReviewerCompletesReview ==", tla)
        self.assertIn("[](~((request'.review_complete # request.review_complete /\\ ~((actor.role = \"reviewer\")))))", tla)
        self.assertIn("request'.review_complete # request.review_complete", tla)
        cfg_lines = cfg.splitlines()
        self.assertIn("PROPERTIES", cfg_lines)
        self.assertIn("NonReviewerCompletesReview", cfg_lines)
        invariants_index = cfg_lines.index("INVARIANTS")
        properties_index = cfg_lines.index("PROPERTIES")
        self.assertNotIn("NonReviewerCompletesReview", cfg_lines[invariants_index:properties_index])

    def test_state_only_forbidden_remains_invariant(self) -> None:
        model = parse_dsl_source(
            '''
workflow("payment_rules")

entity("payment", Record(
    attempts=Int,
))

init(
    Eq(Field("payment", "attempts"), 0),
)

action(
    "increment_attempts",
    changes=[
        SetField("payment", "attempts", Add(Field("payment", "attempts"), 1)),
    ],
)

forbidden("no_negative_attempts", when=Lt(Field("payment", "attempts"), 0))
'''
        )
        tla = lower_to_tla(model)
        cfg = build_cfg(model)

        self.assertIn("NoNegativeAttempts ==", tla)
        self.assertIn("~((payment.attempts < 0))", tla)
        cfg_lines = cfg.splitlines()
        invariants_index = cfg_lines.index("INVARIANTS")
        if "PROPERTIES" in cfg_lines:
            properties_index = cfg_lines.index("PROPERTIES")
            self.assertIn("NoNegativeAttempts", cfg_lines[invariants_index:properties_index])
        else:
            self.assertIn("NoNegativeAttempts", cfg_lines[invariants_index:])
        self.assertNotIn("PROPERTIES\nNoNegativeAttempts", cfg)

    def test_multiple_transition_forbiddens_produce_multiple_properties(self) -> None:
        model = parse_dsl_source(
            '''
workflow("transition_rules")

entity("request", Record(
    status=Enum("draft", "approved", "rejected"),
    review_complete=Bool,
))

init(
    Eq(Field("request", "status"), "draft"),
    Eq(Field("request", "review_complete"), False),
)

action(
    "approve",
    requires=[
        Eq(Field("request", "status"), "draft"),
    ],
    changes=[
        SetField("request", "status", "approved"),
    ],
)

forbidden(
    "approved_status_cannot_change",
    when=And(
        Eq(Field("request", "status"), "approved"),
        Changed(Field("request", "status")),
    ),
)

forbidden(
    "review_complete_cannot_flip_back",
    when=And(
        Eq(Field("request", "review_complete"), True),
        Changed(Field("request", "review_complete")),
    ),
)
'''
        )
        tla = lower_to_tla(model)
        cfg = build_cfg(model)

        self.assertIn("ApprovedStatusCannotChange ==", tla)
        self.assertIn("ReviewCompleteCannotFlipBack ==", tla)
        cfg_lines = cfg.splitlines()
        self.assertIn("PROPERTIES", cfg_lines)
        self.assertIn("ApprovedStatusCannotChange", cfg_lines)
        self.assertIn("ReviewCompleteCannotFlipBack", cfg_lines)

    def test_no_synthetic_violation_remains(self) -> None:
        tla = lower_to_tla(self._model())

        self.assertNotIn("violation", tla)
        self.assertNotIn("NoForbiddenTransitionViolation", tla)

    def test_lowers_obligation_to_property(self) -> None:
        model = self._model()
        tla = lower_to_tla(model)
        cfg = build_cfg(model)

        self.assertIn("SubmittedRequestsEventuallyReviewed ==", tla)
        self.assertIn('[]((request.status = "submitted") => <>request.review_complete)', tla)
        self.assertIn("PROPERTIES", cfg)
        self.assertIn("SubmittedRequestsEventuallyReviewed", cfg)

    def test_lowers_completion_requires_to_property(self) -> None:
        model = parse_dsl_source(
            REVIEW_WORKFLOW
            + """

completion_requires(
    "paid_orders_eventually_ship",
    outcome=Eq(Field("request", "status"), "approved"),
    condition=Eq(Field("request", "status"), "rejected"),
)
"""
        )
        tla = lower_to_tla(model)
        cfg = build_cfg(model)

        self.assertIn("PaidOrdersEventuallyShip ==", tla)
        self.assertIn('[]((request.status = "approved") => <>(request.status = "rejected"))', tla)
        cfg_lines = cfg.splitlines()
        self.assertIn("PROPERTIES", cfg_lines)
        self.assertIn("PaidOrdersEventuallyShip", cfg_lines)
        invariants_index = cfg_lines.index("INVARIANTS")
        properties_index = cfg_lines.index("PROPERTIES")
        self.assertNotIn("PaidOrdersEventuallyShip", cfg_lines[invariants_index:properties_index])

    def test_generated_tla_passes_sanity_checker(self) -> None:
        model = self._model()
        tla = lower_to_tla(model)
        cfg = build_cfg(model)
        tla_sanity = _load_tla_sanity_module()

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            tla_path = root / "GenericReviewWorkflow.tla"
            cfg_path = root / "GenericReviewWorkflow.cfg"
            tla_path.write_text(tla, encoding="utf-8")
            cfg_path.write_text(cfg, encoding="utf-8")

            tla_sanity.sanitize_tla_cfg_pair(tla_path, cfg_path)
            rewritten = tla_path.read_text(encoding="utf-8")

            self.assertIn("---- MODULE GenericReviewWorkflow ----", rewritten)
            self.assertIn("Init ==", rewritten)
            self.assertIn("Next ==", rewritten)
            self.assertIn("Spec ==", rewritten)

    def test_warned_models_still_lower(self) -> None:
        model = parse_dsl_source(
            '''
workflow("warned_model")

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
        )
        tla = lower_to_tla(model)
        cfg = build_cfg(model)

        self.assertTrue(model.warnings)
        self.assertIn("PaymentAttemptSucceeds ==", tla)
        self.assertIn("SPECIFICATION Spec", cfg)

    def test_lowers_checked_events_to_bounded_event_state(self) -> None:
        model = parse_dsl_source(
            '''
workflow("refund_events")

entity("refund", Record(
    status=Enum("requested", "refunded", "notified"),
))

init(
    Eq(Field("refund", "status"), "requested"),
)

action(
    "process_refund",
    requires=[
        Eq(Field("refund", "status"), "requested"),
    ],
    changes=[
        SetField("refund", "status", "refunded"),
    ],
    emits=[
        "refund_payment_issued",
        "ledger_entry_created",
    ],
)

action(
    "send_confirmation",
    requires=[
        Eq(Field("refund", "status"), "refunded"),
    ],
    changes=[
        SetField("refund", "status", "notified"),
    ],
    emits=[
        "refund_confirmation_sent",
    ],
)

obligation(
    "refund_payment_eventually_notifies_user",
    trigger=Emitted("refund_payment_issued"),
    must_eventually=Emitted("refund_confirmation_sent"),
)

invariant(
    "notification_only_after_refund_payment",
    Implies(
        Emitted("refund_confirmation_sent"),
        SeenEvent("refund_payment_issued"),
    ),
)
'''
        )
        tla = lower_to_tla(model)
        cfg = build_cfg(model)
        warnings = tla_lowering_warnings(model)

        self.assertIn('EventDomain == {"refund_confirmation_sent", "refund_payment_issued"}', tla)
        self.assertNotIn("ledger_entry_created", tla)
        self.assertIn("VARIABLES refund, emitted, seen_events", tla)
        self.assertIn("vars == << refund, emitted, seen_events >>", tla)
        self.assertIn("emitted \\in SUBSET EventDomain", tla)
        self.assertIn("seen_events \\in SUBSET EventDomain", tla)
        self.assertIn("emitted = {}", tla)
        self.assertIn("seen_events = {}", tla)
        self.assertIn('emitted\' = {"refund_payment_issued"}', tla)
        self.assertIn("seen_events' = seen_events \\cup emitted'", tla)
        self.assertIn('emitted\' = {"refund_confirmation_sent"}', tla)
        self.assertIn('("refund_payment_issued" \\in emitted)', tla)
        self.assertIn('("refund_confirmation_sent" \\in emitted)', tla)
        self.assertIn('("refund_payment_issued" \\in seen_events)', tla)
        self.assertIn("RefundPaymentEventuallyNotifiesUser", cfg)
        self.assertIn("NotificationOnlyAfterRefundPayment", cfg)
        self.assertTrue(any(w.startswith("W_TLA_EMITS_NOT_CHECKED: action process_refund") for w in warnings))

    def test_unchecked_emits_do_not_change_generated_tla(self) -> None:
        with_emit = parse_dsl_source(
            '''
workflow("unchecked_emit")

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
        "finish_notification_sent",
    ],
)
'''
        )
        without_emit = parse_dsl_source(
            '''
workflow("unchecked_emit")

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
)
'''
        )

        self.assertEqual(lower_to_tla(with_emit), lower_to_tla(without_emit))
        warnings = tla_lowering_warnings(with_emit)
        self.assertTrue(any(w.startswith("W_TLA_EMITS_NOT_CHECKED") for w in warnings))

    def test_ensures_warn_but_do_not_change_generated_tla(self) -> None:
        with_ensures = parse_dsl_source(
            '''
workflow("ensure_warning")

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
    ensures=[
        Eq(Field("task", "status"), "done"),
    ],
)
'''
        )
        without_ensures = parse_dsl_source(
            '''
workflow("ensure_warning")

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
)
'''
        )

        self.assertEqual(lower_to_tla(with_ensures), lower_to_tla(without_ensures))
        warnings = tla_lowering_warnings(with_ensures)
        self.assertTrue(any(w.startswith("W_TLA_ENSURES_NOT_CHECKED") for w in warnings))


if __name__ == "__main__":
    unittest.main()
