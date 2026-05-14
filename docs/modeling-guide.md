# Modeling Guide

invari-spec works best with workflow-style prose that names:

- states
- actions or transitions
- success conditions
- failure or fallback paths
- retry behavior
- completion requirements

Current v1 input is markdown, but the model is intentionally general enough to support design docs and requirements docs that describe workflow behavior.

When the source text is ambiguous, invari-spec preserves that ambiguity as nondeterminism or exploration warnings rather than inventing missing logic.

## Property and invariant constructs

- `invariant(...)`: an always-true state predicate. It lowers to a TLA+ invariant and is checked under `INVARIANTS`.
- `completion_requires(...)`: a same-state completion safety requirement. `completion_requires(outcome=X, condition=Y)` means that whenever `X` is true, `Y` must already be true in that same state; it is checked under `INVARIANTS`.
- `forbidden(...)`: an invalid state or transition pattern. State-only forbidden predicates lower to invariants; transition-aware predicates, such as predicates using `Changed(...)`, remain temporal properties under `PROPERTIES`.
- `obligation(...)`: an eventual progress or liveness requirement. `obligation(trigger=X, must_eventually=Y)` means that if `X` occurs, `Y` must eventually occur; it is checked under `PROPERTIES`.

## Fairness assumptions

Action declarations may include `fairness="weak"` or `fairness="strong"` when a liveness obligation depends on internal system progress. Use weak fairness for actions that should eventually run while continuously enabled, such as retry dispatch, fallback routing, or completion after prerequisites are met. Use strong fairness only when the model explicitly needs an internal action that is enabled intermittently to eventually run, such as polling or background reconciliation.

Do not add fairness to user choices, external service outcomes, approvals, payments, or random failures. Those are environment-controlled decisions and should remain nondeterministic branches.
