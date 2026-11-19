# ADR-002: Queue-level pause on gateway outage instead of per-item failure

## Status
Adopted

## Background
The ticket explicitly requires: "when the model gateway is unavailable, the
entire queue is paused rather than failing item by item."

## Decision
The worker mirrors gateway health into a Redis flag (`intel:queue:paused`).
While set, `run_once` pulls nothing; items stay queued with status `pending`.
On recovery the flag is cleared and processing resumes. The gateway stub exposes
`is_unavailable`, which the real gateway ticket will back with its health check.

## Alternatives considered
- Per-item failure marking: explicitly rejected — an outage would burn through
  the queue falsely marking every item processing_failed, then require a bulk
  reprocess of healthy items.

## Consequences
- In-flight items finish; no partial-state items during an outage.
- Pause state lives in Redis, so multiple worker processes share one queue-level
  decision (no per-process divergence).
- Recovery is manual-flag-clear + gateway health; a future ticket can automate
  the flag from gateway health probes.

## Source
Ticket requirement S5-INTEL-023; engineering standard 06.2 (circuit breaker).
