# Skill: infinite_retry

## Goal
Run a task that retries whenever an attempt fails.

## Workflow
1. Start in the ready state.
2. Begin an attempt.
3. If the attempt succeeds, mark the task as succeeded.
4. If the attempt fails, mark the task as failed.
5. Retry after every failure.

## Required behavior
- The workflow should eventually succeed.
- Retry count records how many failed attempts have happened.
- The workflow does not define a maximum retry count.

## Failure handling
- Every failure is retried.
- There is no fallback path.

## Completion
- The only successful completion state is succeeded.
