# Skill: valid_retry_with_fallback

## Goal
Run a task that may fail, retry it a bounded number of times, and use a fallback when retries are exhausted.

## Workflow
1. Start in the ready state.
2. Begin an attempt.
3. If the attempt succeeds, mark the task as succeeded.
4. If the attempt fails and retry count is below 3, increment retry count.
5. Retry the task after a retryable failure.
6. If the attempt fails after retries are exhausted, use fallback.

## Required behavior
- Retry count must never exceed 3.
- A failed attempt may only be retried while retry count is below 3.
- A task may finish by succeeding or by entering fallback.

## Failure handling
- Failure before the retry limit should be retried.
- Failure at the retry limit should enter fallback.

## Completion
- The workflow should eventually reach succeeded or fallback.
