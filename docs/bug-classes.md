# Bug Classes

Speclens v1 focuses on a small set of high-signal findings:

- missing fallback
- unreachable success
- infinite retry or stalled completion
- transition-level forbidden behavior
- underspecified assumptions
- fairness-sensitive liveness failures

Liveness failures are reported separately from fairness gaps. If a model contains obligations but no explicit fairness assumptions, Speclens marks the result as fairness-sensitive instead of presenting every stuttering counterexample as a confirmed workflow bug.
