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
