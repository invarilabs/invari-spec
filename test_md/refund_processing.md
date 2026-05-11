# What this does

This workflow handles refund requests for customer orders. It checks eligibility, processes the refund, and notifies the user.

# How it works

When a user requests a refund, the system first checks if the order exists and was successfully delivered.

Next, it verifies whether the refund request is within 30 days of purchase. If it’s outside that window, the request should not proceed.

Then the system checks if the item is refundable:
- Digital items are not refundable once downloaded.
- Physical items must not be marked as used or damaged.

If the request passes these checks, the system processes the refund by issuing the payment back to the user.

After processing the refund, the system sends a confirmation notification to the user.

# Things to keep in mind

Refunds should only be processed for valid, delivered orders.
Refunds should not be issued for ineligible items.
Notifications should always be sent after a refund is processed.

# Edge cases

A refund request might come in exactly on the 30-day boundary.
The item status might change while the refund is being processed.
Payment processing might fail temporarily.

# Example

User: “I want a refund for this order.”
System: Checks eligibility → processes refund → sends confirmation