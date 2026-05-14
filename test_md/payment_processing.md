# Skill: Payment Processing Agent

## Overview

The payment processing agent orchestrates the full lifecycle of a card payment on behalf of a user completing a purchase. It coordinates with the payment gateway, manages state transitions, handles transient failures, and ensures funds only move when all conditions are satisfied.

---

## Payment States

A payment moves through the following named states:

- **pending** — payment record created, no gateway interaction yet
- **authorized** — gateway has reserved funds on the card; awaiting capture
- **captured** — funds have been successfully charged to the card
- **voided** — authorization was released; no charge was made
- **refunded** — captured funds have been returned to the customer
- **failed** — a terminal failure; no further transitions are allowed

---

## Actions

### authorize
Request the gateway to reserve funds on the customer's card.

**Requires:**
- Payment is in `pending` state
- Payment amount is greater than zero

**Gateway outcome determines next state:**
- If the gateway approves: move to `authorized`
- If the gateway declines: move to `failed`

---

### capture
Charge the reserved funds after the order has been confirmed.

**Requires:**
- Payment is in `authorized` state

**Gateway outcome determines next state:**
- If the gateway succeeds: move to `captured`
- If the gateway fails: move to `failed`

---

### void
Release an authorization without charging the customer. Used when an order is cancelled after authorization but before capture.

**Requires:**
- Payment is in `authorized` state

**Effect:** Move to `voided`

---

### refund
Return captured funds to the customer.

**Requires:**
- Payment is in `captured` state

**Gateway outcome determines next state:**
- If the gateway succeeds: move to `refunded`
- If the gateway fails: payment remains in `captured`; agent retries up to 3 times before giving up

---

## Rules and Invariants

- A payment must never be captured without first being authorized.
- A refund must never be issued on a payment that has not been captured.
- A voided payment must never be captured or refunded.
- A failed payment is terminal — no further transitions are allowed from `failed`.
- Retry count must never exceed the configured maximum of 3.

---

## External Outcomes

The following are determined by the payment gateway and are not under the agent's control:

- Whether an authorization request succeeds or fails
- Whether a capture request succeeds or fails
- Whether a refund request succeeds or fails

---

## Obligations

- A payment that has been authorized must eventually reach a terminal state: `captured`, `voided`, or `failed`.
- A payment that has been captured must eventually reach `refunded` or remain permanently in `captured` (if no refund is requested).

---

## Edge Cases

- An order may be cancelled after authorization but before capture — the agent must void the authorization to release the hold on the customer's card.
- A capture may fail after a successful authorization — the authorization hold remains on the customer's card until it expires if the agent does not explicitly void it.
- Refund retries may all fail — the agent should not loop indefinitely.
- The gateway may return a transient error that resolves on retry, or a hard decline that will not resolve regardless of retries.
