from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

from invari_spec.semantic_dsl import build_cfg, lower_to_tla, parse_dsl_source
try:
    from tests.test_semantic_dsl_parser import REVIEW_WORKFLOW
except ImportError:
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

    def test_lowers_action_fairness_into_spec(self) -> None:
        model = parse_dsl_source(
            REVIEW_WORKFLOW.replace(
                'action(\n    "complete_review",',
                'action(\n    "complete_review",\n    fairness="weak",',
                1,
            ).replace(
                'action(\n    "approve_request",',
                'action(\n    "approve_request",\n    fairness="strong",',
                1,
            )
        )

        tla = lower_to_tla(model)

        self.assertIn("Spec ==", tla)
        self.assertIn("Init /\\ [][Next]_vars", tla)
        self.assertIn("/\\ WF_vars(CompleteReview)", tla)
        self.assertIn("/\\ SF_vars(ApproveRequest)", tla)

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

entity("order", Record(
    status=Enum("CREATED", "FAILED"),
))

var("order_exists", Bool)

init(
    Eq(Field("order", "status"), "CREATED"),
    Eq(Var("order_exists"), False),
)

action(
    "process_order",
    requires=[
        Not(Var("order_exists")),
    ],
    changes=[
        Set("order_exists", True),
    ],
)
'''
        )
        tla = lower_to_tla(model)
        cfg = build_cfg(model)

        self.assertTrue(model.warnings)
        self.assertIn("ProcessOrder ==", tla)
        self.assertIn("SPECIFICATION Spec", cfg)


if __name__ == "__main__":
    unittest.main()
