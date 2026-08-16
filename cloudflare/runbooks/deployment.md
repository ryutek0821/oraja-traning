# Deployment runbook

## Preconditions

- The `preview`, `staging`, and `production` GitHub Environments exist.
- `production` has required reviewers configured. A workflow file alone is
  not an approval policy.
- Each environment has its own `CLOUDFLARE_API_TOKEN` and
  `CLOUDFLARE_ACCOUNT_ID` secrets. Secret values must not be copied into
  `wrangler.jsonc`, `vars`, or issue comments.
- The exact custom domain, DNS ownership, and Cloudflare resource inventory
  have been approved outside this repository. The `.example.invalid` hosts
  in the config are placeholders until that approval exists.
- A complete isolated restore drill exists as specified by
  [backup-restore-drill.md](backup-restore-drill.md). A backup ID without a
  successful restore is not sufficient.

## Verification and deployment

1. Run the CI checks, including `npm run deploy:preview:dry`, on the commit to
   be deployed. Record the commit SHA, SBOM artifact, migration list, and
   binding/resource names.
2. Start the `Cloudflare deploy` workflow manually and select `preview`.
   Confirm `/healthz` returns `status=ok` and `/version` reports the selected
   commit before any staging promotion.
3. Select `staging` only after preview checks pass. Supply the approved issue,
   backup evidence, restore-drill evidence, previous rollback commit, release
   manifest digest, migration-plan digest, and an explicit `go` decision. The
   deploy script rejects missing or malformed evidence before invoking
   Wrangler. Verify that the D1, DO,
   R2, Queue, Workflow, Container, route, and account IDs match the staging
   inventory and not preview or production.
4. Select `production` only when the release record is approved. GitHub
   pauses the job at the protected production Environment; after the
   reviewer approves, `guarded-deploy.mjs` requires the explicit approval
   flag, the two production secrets, and the same fail-closed evidence set.
5. Recheck `/healthz`, `/version`, binding access, and migration state. Store
   the Worker revision and build metadata in the deployment record.

The workflow is `workflow_dispatch` only. This issue does not create
Cloudflare resources or perform a deployment.

## Rollback

Stop writes, keep the read-only health/version endpoints available, and use
the migration rollback plan in [migration-rollback.md](migration-rollback.md).
Restore the approved environment backup, deploy the previous approved Worker
revision through the same environment gate, verify bindings, and reopen
writes only after the checks pass. Never edit an applied migration or delete
immutable artifacts to make a rollback appear successful.

Rollback is considered complete only after the isolation negative suite,
immutable revision pointers, deletion/tombstone state, and D1/DO/R2 inventory
digests match the approved recovery manifest. Record discrepancies as an
incident; do not reopen writes to make an SLO appear healthy.
