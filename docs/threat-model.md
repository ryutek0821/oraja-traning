# Threat model

## Assets and boundaries

| Asset | Owner | Boundary | Required control |
|---|---|---|---|
| credentials, recovery codes | Account | D1 control plane | Argon2id/hash-only storage, rate limits, single use |
| play events and model state | Profile | Profile Durable Object | account/profile/device predicates and transaction idempotency |
| submitted 5DB and artifacts | Profile | private R2 | envelope encryption, profile prefix, digest deduplication |
| approved advisor journal | Profile | D1 + MCP | pending-by-default, explicit scope and approval |
| aggregate model | Official service | official trust domain | self-hosted/withdrawn data rejection |

## Threats and mitigations

- Cross-account identifiers are never authorization input; ownership comes from
  the authenticated subject and composite D1 predicates.
- Replay and duplicate delivery use UUIDv7 event IDs, payload digests, and a
  profile-local transaction before Queue enqueue.
- Capability URLs contain only a random secret, are stored as hashes, and are
  revoked by rotation or profile deletion.
- Uploads reject SQLite sidecars, oversized files, unknown names, malformed
  manifests, and plaintext envelope metadata.
- MCP exposes aggregate resources and explicit paged tools only; it never
  returns raw DB objects or conversation transcripts.

Residual risk: real Cloudflare binding configuration, secret rotation, restore
drills, and multi-client OAuth testing must be completed in staging before any
production approval.
