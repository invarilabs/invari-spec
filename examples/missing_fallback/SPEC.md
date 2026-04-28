# Skill: missing_fallback

## Goal
Call an external service and handle both success and failure.

## Workflow
1. Start in the ready state.
2. Call the external service.
3. If the service succeeds, mark the task as succeeded.
4. If the service fails, mark the task as failed.

## Required behavior
- The service call can succeed or fail.
- A successful service call should reach succeeded.
- A failed service call must be handled.

## Failure handling
- Failure should eventually be handled or routed to fallback.
- The workflow does not define the recovery or fallback step.

## Completion
- The workflow is complete only when it succeeds or the failure is handled.
