# Contributing

1. Read `SPEC.md`, `docs/architecture.md`, and the relevant contract before
   changing a boundary.
2. Use synthetic fixtures only. Never add `player-file`, real databases,
   personal paths, secrets, replay data, or internal prompts.
3. Keep account/profile ownership predicates explicit and preserve the
   `official` versus `self-hosted` trust boundary.
4. Run `UV_CACHE_DIR=/private/tmp/oraja-uv-cache uv run pytest -q` and, for
   Cloudflare changes, `npm run check`, `npm test`, `npm run typecheck`, and
   `npm run lint` from `cloudflare/`.
5. Document schema/API changes and include a rejection example when a new
   contract boundary is introduced.

Production deployment, migration application, repository publication, and
general registration require a separate explicit approval.
