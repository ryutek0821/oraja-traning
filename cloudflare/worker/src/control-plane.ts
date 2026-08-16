export type ProfileRecord = {
  id: string;
  public_id: string;
  account_id: string;
  display_name: string;
  timezone: string;
  status: string;
};

export type CreateProfile = {
  id: string;
  publicId: string;
  accountId: string;
  displayName: string;
  timezone: string;
  now: number;
};

export type AuditEvent = {
  id: string;
  accountId: string | null;
  profileId: string | null;
  actorKind: "account" | "device" | "service" | "oauth";
  eventType: string;
  reasonCode?: string;
  requestId?: string;
  resourceHash?: string;
  inputHash?: string;
  status: string;
  occurredAt: number;
};

/**
 * All reads take the authenticated account owner as a parameter. Callers must
 * never construct an owner predicate from a request body or URL segment.
 */
export class D1ControlPlane {
  constructor(private readonly db: D1Database) {}

  async getOwnedProfile(
    accountId: string,
    profileId: string,
  ): Promise<ProfileRecord | null> {
    return this.db
      .prepare(
        `SELECT id, public_id, account_id, display_name, timezone, status
           FROM profiles
          WHERE account_id = ?1 AND id = ?2
            AND status <> 'deleted'`,
      )
      .bind(accountId, profileId)
      .first<ProfileRecord>();
  }

  async createProfile(input: CreateProfile): Promise<ProfileRecord> {
    const statements = [
      this.db
        .prepare(
          `INSERT OR IGNORE INTO profile_limits(account_id, max_profiles, updated_at)
           VALUES (?1, 1, ?2)`,
        )
        .bind(input.accountId, input.now),
      this.db
        .prepare(
          `INSERT INTO profiles(
             id, public_id, account_id, display_name, timezone,
             status, created_at, updated_at
           ) VALUES (?1, ?2, ?3, ?4, ?5, 'active', ?6, ?6)`,
        )
        .bind(
          input.id,
          input.publicId,
          input.accountId,
          input.displayName,
          input.timezone,
          input.now,
        ),
    ];
    await this.db.batch(statements);
    const profile = await this.getOwnedProfile(input.accountId, input.id);
    if (!profile) throw new Error("profile_create_missing");
    return profile;
  }

  async appendAuditEvent(event: AuditEvent): Promise<void> {
    await this.db
      .prepare(
        `INSERT INTO audit_events(
           id, account_id, profile_id, actor_kind, event_type,
           reason_code, request_id, resource_hash, input_hash, status, occurred_at
         ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11)`,
      )
      .bind(
        event.id,
        event.accountId,
        event.profileId,
        event.actorKind,
        event.eventType,
        event.reasonCode ?? null,
        event.requestId ?? null,
        event.resourceHash ?? null,
        event.inputHash ?? null,
        event.status,
        event.occurredAt,
      )
      .run();
  }
}
