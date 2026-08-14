# Backup / restore drill

This drill is mandatory before staging or production promotion. It uses only
synthetic fixtures and an isolated recovery environment. A production backup
has not been proven merely because an export command returned successfully.

## Scope and evidence contract

The release owner records one immutable drill ID and the exact source revision,
environment, D1 export digest, encrypted R2 inventory digest, Durable Object
export digest, encryption-key version (never the key), start/end time, operator,
reviewer, and result. The drill ID is supplied as
`ORAJA_RESTORE_DRILL_EVIDENCE`; the pre-deploy backup record is supplied as
`ORAJA_BACKUP_EVIDENCE`.

Hard stops:

- use of production bindings as the restore destination;
- missing Account/Profile ownership metadata or encryption-key version;
- plaintext export outside an access-controlled temporary workspace;
- inability to export and restore every Durable Object namespace;
- digest, row/object count, revision pointer, or deletion-state mismatch;
- a restore path that relies on editing an applied migration.

## Drill

1. Freeze the source release/revision and record `npm run migration:plan` plus
   its SHA-256. Seed a dedicated source environment with synthetic profiles,
   accepted and duplicate plays, immutable table revisions, a pending deletion,
   and encrypted raw artifacts.
2. Export D1 consistently, enumerate all R2 keys with version/digest metadata,
   and invoke the application-owned Durable Object export path. Encrypt each
   export before it leaves the temporary workspace. Record counts and digests,
   not payloads, in the drill record.
3. Create new isolated D1/R2/DO resources. Apply the same numbered migrations
   in order, import D1, restore encrypted objects without changing immutable
   keys, and import each DO through its owner-checked restore path.
4. Run health/version checks and the account/profile negative suite. Compare
   D1 row counts, R2 inventory digests, DO event counts, latest revision
   pointers, tombstones, and export/delete state to the source manifest.
5. Submit one duplicate play and one new synthetic play. The duplicate count
   must remain one, the new event must advance normally, and no other profile
   may observe either event.
6. Destroying recovery resources is a separate destructive operation requiring
   explicit approval. Until then, revoke recovery credentials and retain the
   encrypted evidence according to the security retention policy.

## Current implementation gate

The drill must be marked **blocked**, not passed, while any application-owned
D1/DO/R2 export or restore path is absent. Manual copying through a dashboard,
or validating D1 while omitting DO/R2, is not acceptable evidence. The deploy
gate intentionally fails closed until a real drill ID and backup ID are
provided.
