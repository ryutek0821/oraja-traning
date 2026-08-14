# Cloudflare foundation

This directory is the deployable Cloudflare workspace for the official
service. It keeps the Worker, static Web assets, Python Container, and future
IR module boundaries separate:

| Path | Responsibility |
|---|---|
| `worker/` | Worker/API, Durable Objects, Queue/Workflow entry points |
| `web/` | static asset boundary; product UI is added by issue #15 |
| `container/` | pinned Python processor image and health endpoint |
| `ir/` | Java 17 `IRConnection`, durable spool, HTTPS transport, and compatibility tests |
| `migrations/` | ordered D1 migrations |
| `runbooks/` | operator procedures; no credentials or resource IDs |

`wrangler.jsonc` is the binding source of truth. Every environment has a
distinct Worker/resource name and custom-domain placeholder. The binding
names are intentionally stable inside an environment (`CONTROL_DB`,
`PROFILE_DO`, `RAW_BUCKET`, and so on), while the underlying D1, R2, Queue,
Workflow, Container, and route names carry the environment suffix. Account IDs
are supplied by environment-scoped GitHub secrets; they are not committed.

## Local verification

From this directory:

```sh
npm ci --ignore-scripts
npm run lint
npm test
npm run schema:check
npm run check
npm run typecheck
npm run container:build
npm run deploy:preview:dry
```

`deploy:*:dry` builds the Worker and Container but does not publish a
revision. It injects `BUILD_VERSION` from the environment when present, so a
CI run can trace `/version` to its commit without putting secrets in config.
The SBOM command is `npm run sbom` and uses the committed lockfile.

The Worker exposes `GET /healthz` and `GET /version`; the Container exposes
the same paths. A successful health response is not a binding migration or
data-readiness signal, so migration state must be checked separately.

Production is never part of CI push/PR jobs. Use the manual
`Cloudflare deploy` workflow; its `production` job is protected by the
GitHub Environment reviewer gate and the guarded deploy script. Migration
apply and preview cleanup remain explicit operator actions described in the
runbooks.
