# Migration / rollback runbook

1. Review the ordered migration list with `npm run migration:plan`.
2. Apply numbered D1 migrations in ascending order to the selected named
   environment only after the operator and environment approvals are present:

   ```sh
   ORAJA_MIGRATIONS_APPLY_APPROVED=true \
     npm run migration:apply -- preview --confirm
   ```

   Replace `preview` only with the explicitly approved environment. The
   script uses the named `CONTROL_DB` binding and `--remote`; it is not run by
   pull-request CI.
3. Record the Worker version, migration list and its SHA-256, binding names,
   pre-deploy backup ID, isolated restore-drill ID, previous approved 40-character
   commit SHA, release-manifest SHA-256, operator, and approver. These values
   are required by the staging/production deploy evidence gate.
4. If a migration is incompatible, stop writes, keep read-only health and
   version endpoints available, restore the last approved backup, and deploy
   the previously approved Worker version.
5. Follow [backup-restore-drill.md](backup-restore-drill.md) and compare the
   restored D1 rows, Durable Object state, encrypted R2 inventory, tombstones,
   owner boundaries, and immutable latest pointers before reopening writes.
6. Never edit an applied migration. Add a forward repair migration after the
   restore has been verified.

The foundation migration is intentionally a no-op marker. Control-plane
schema and data migrations are added by issue #6 and must include a matching
rollback note before staging deployment.
