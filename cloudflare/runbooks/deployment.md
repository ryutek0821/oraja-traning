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

## Verification and deployment

1. Run the CI checks, including `npm run deploy:preview:dry`, on the commit to
   be deployed. Record the commit SHA, SBOM artifact, migration list, and
   binding/resource names.
2. Start the `Cloudflare deploy` workflow manually and select `preview`.
   Confirm `/healthz` returns `status=ok` and `/version` reports the selected
   commit before any staging promotion.
3. Select `staging` only after preview checks pass. Verify that the D1, DO,
   R2, Queue, Workflow, Container, route, and account IDs match the staging
   inventory and not preview or production.
4. Select `production` only when the release record is approved. GitHub
   pauses the job at the protected production Environment; after the
   reviewer approves, `guarded-deploy.mjs` requires the explicit approval
   flag and the two production secrets.
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
