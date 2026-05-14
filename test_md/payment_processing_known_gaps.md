# Payment Processing — Known Gaps (Reference)

These are gaps identified by manual review of `payment_processing.md` before running invari-spec.
Use this to compare against what the pipeline actually finds.

---

**G1 — Capture failure leaves a dangling authorization**
When capture fails, the spec moves the payment to `failed` but does not require the agent to void the outstanding authorization first. The customer's card hold may remain active until the gateway's authorization window expires (typically 7 days). It is unclear whether the agent is responsible for explicitly voiding on capture failure.

**G2 — Refund "giving up" is undefined**
After 3 failed refund retries the spec says the agent "gives up" but does not define what state the payment ends up in, whether it escalates to a human operator, or whether the customer is notified. The payment would remain indefinitely in `captured` with no exit path.

**G3 — `failed` is overloaded**
Both authorization failure and capture failure resolve to `failed`, but they have different implications. After an auth failure no card hold exists. After a capture failure an authorization hold may still be active. A single `failed` state does not let the agent distinguish these cases, which may affect what cleanup actions are needed.

**G4 — No retry defined for authorization or capture**
Retry logic is specified only for refunds. The spec does not say whether authorization or capture failures should be retried before moving to `failed`, or what the retry limits would be.

**G5 — Void failure is unhandled**
The void action has no defined failure path. If the gateway fails to release the authorization, the spec does not say what state the payment moves to or what the agent should do next.

**G6 — `captured` is ambiguous as a terminal state**
The obligations state that a captured payment "remains permanently in `captured` if no refund is requested." This makes `captured` both a resting state and an intermediate state. It is unclear whether a payment stuck in `captured` indefinitely is expected behavior or a gap.
