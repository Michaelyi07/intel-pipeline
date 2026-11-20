# ADR-002: Queue-level pause on gateway outage, automatic recovery, resumable mid-item outage

## Status
Adopted (revised: recovery is automatic; mid-item outages requeue the item)

## Background
The ticket explicitly requires: "when the model gateway is unavailable, the
entire queue is paused rather than failing item by item" and "processing
resumes after recovery".

## Decision
The worker mirrors gateway health into a Redis flag (`intel:queue:paused`) in
BOTH directions, every cycle:

- gateway unavailable → flag set (queue paused, nothing pulled);
- gateway healthy while the flag is set → flag cleared automatically, so a
  recovered queue resumes on the next worker cycle without any manual action.

When an item is being processed and the gateway turns unavailable mid-item,
the orchestrator treats it as a distinct exception (`GatewayUnavailableError`,
a `ConnectionError` subclass raised by the gateway client/stub), NOT an
ordinary failure:

- the current attempt is recorded as `failed` with `error_code=gateway_unavailable`
  (audit trail preserved);
- no retry/backoff burn, no `skipped` markers;
- the item status returns to `pending` (resumable) and the worker requeues it
  at the head of the queue, then pauses the queue.

On resume, `_first_pending_step` picks up from the first step without a
`succeeded` row, so completed steps are never re-executed. The gateway stub
exposes `is_unavailable`; the real gateway ticket backs it with its health
check (or raises `GatewayUnavailableError` from its client).

## Alternatives considered
- Per-item failure marking: explicitly rejected — an outage would burn through
  the queue falsely marking every item processing_failed, then require a bulk
  reprocess of healthy items.
- Manual flag clear on recovery: rejected (v1 of this ADR) — the client review
  correctly flagged that nothing in the code cleared the flag, so a paused
  queue would stay paused forever in a real deployment.

## Consequences
- In-flight items affected by an outage are requeued and resume; no item is
  ever marked `processing_failed` because of a gateway outage.
- Pause state lives in Redis, so multiple worker processes share one
  queue-level decision (no per-process divergence).
- Recovery requires no operator action; a healthy gateway + worker cycle
  clears the flag.

## Source
Ticket requirement S5-INTEL-023; client review of the 2026-11-19 delivery;
engineering standard 06.2 (circuit breaker).
