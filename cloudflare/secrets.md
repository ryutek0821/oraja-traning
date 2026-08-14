# Secret inventory

Secret values are never committed to the repository, Wrangler `vars`, or CI
logs. Values are provisioned per environment through the Cloudflare dashboard
or the matching GitHub Environment secret store. The account ID is not in
`wrangler.jsonc`: the selected GitHub Environment supplies the account to
Wrangler, so preview, staging, and production cannot silently share one
account credential.

| Name | Scope | Rotation owner | Initial use |
|---|---|---|---|
| `CLOUDFLARE_API_TOKEN` | each GitHub Environment (`preview`, `staging`, `production`) | repository owner | Wrangler deploy/authentication |
| `CLOUDFLARE_ACCOUNT_ID` | each GitHub Environment (`preview`, `staging`, `production`) | repository owner | selected Cloudflare account |
| `SESSION_SIGNING_KEY` | Worker secret | service owner | issue #7 |
| `EMAIL_ENCRYPTION_KEY` | Worker secret | service owner | issue #7 optional email recovery |
| `ENVELOPE_MASTER_KEY` | Worker/Container secret | service owner | issue #9 |
| `CAPABILITY_SECRET_KEY` | Worker secret | service owner | issue #14 |

## Provisioning and rotation

1. Create the three GitHub Environments and configure required reviewers on
   `production` before enabling the deploy workflow.
2. Add the two Cloudflare GitHub secrets to each environment. Use separate
   least-privilege API tokens and account IDs; never put them in repository
   secrets shared by all environments.
3. Put Worker secrets with `wrangler secret put <NAME> --env <environment>`
   from an approved operator session. The value is entered interactively and
   is not written to this repository or command history.
4. On rotation, create and verify the replacement value, update one
   environment at a time, run `/healthz` and `/version`, then revoke the old
   token/key only after the deployment record confirms the new revision.

Production deployment is manual (`workflow_dispatch`) and pauses at the
protected `production` Environment approval gate. The production secret names
are intentionally identical to the non-production names, but their values
must remain in the separate production Environment.
