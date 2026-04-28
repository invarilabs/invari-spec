# Skill: unreachable_success

## Goal
Review a request and complete the workflow successfully after review.

## Workflow
1. Start with a draft request.
2. Submit the request for review.
3. Complete the review.
4. A reviewed request may be rejected.

## Required behavior
- The workflow should be able to complete successfully after review.
- Review must be complete before success.
- A request can be rejected after review.

## Failure handling
- Rejected requests are terminal.

## Completion
- The intended successful completion state is succeeded.
- The workflow does not define an approval or mark-succeeded step.
