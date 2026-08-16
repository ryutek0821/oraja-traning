# Service-level objectives

The following are staging and production gates for the initial free profile:

| Operation | SLO |
|---|---:|
| IR accepted-play ACK p95 | ≤ 2 s |
| accepted play to latest two tables p95 | ≤ 60 s |
| capability table GET p95 | ≤ 500 ms |
| duplicate accepted play rate | 0 |
| cross-profile access | 0 |
| critical data loss or boundary violation | 0 |

Measure request ID, account/profile hash, status, latency, Queue age, retry
count, and revision without logging secrets or payloads. Page on any boundary
violation, sustained error budget burn, restore mismatch, or duplicate rate
above zero. The incident runbook must stop registration and ingestion while
preserving existing read-only tables.
