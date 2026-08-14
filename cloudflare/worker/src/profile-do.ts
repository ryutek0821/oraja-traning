import { DurableObject } from "cloudflare:workers";
import type { Env } from "./index";
import type { NormalizedIrEvent, PlayAck } from "./ir-api";

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "cache-control": "no-store",
};

type ProfileStorage = {
  get<T>(key: string): Promise<T | undefined>;
  put<T>(key: string, value: T): Promise<void>;
  list<T>(options?: { prefix?: string; start?: string; limit?: number }): Promise<Map<string, T>>;
  deleteAll(): Promise<void>;
  transaction?<T>(callback: (storage: ProfileStorage) => Promise<T>): Promise<T>;
};

type StoredPlayEvent = {
  event_id: string;
  payload_digest: string;
  profile_internal_id: string;
  device_internal_id: string;
  job_id: string;
  revision: number;
  accepted_at: number;
  event: NormalizedIrEvent;
  enqueued_at: number | null;
};

type AcceptInput = {
  profile_internal_id: string;
  device_internal_id: string;
  payload_digest: string;
  request_id: string;
  event: NormalizedIrEvent;
};

function response(value: unknown, status = 200): Response {
  return new Response(JSON.stringify(value) + "\n", { status, headers: JSON_HEADERS });
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAcceptInput(value: unknown): value is AcceptInput {
  if (!isRecord(value) || !isRecord(value.event)) return false;
  return (
    typeof value.profile_internal_id === "string" &&
    typeof value.device_internal_id === "string" &&
    typeof value.payload_digest === "string" &&
    /^[0-9a-f]{64}$/.test(value.payload_digest) &&
    typeof value.request_id === "string" &&
    typeof value.event.event_id === "string"
  );
}

function isStoredPlayEvent(value: unknown): value is StoredPlayEvent {
  if (!isRecord(value) || !isRecord(value.event)) return false;
  return (
    typeof value.event_id === "string" &&
    typeof value.payload_digest === "string" &&
    typeof value.profile_internal_id === "string" &&
    typeof value.device_internal_id === "string" &&
    typeof value.job_id === "string" &&
    typeof value.revision === "number" &&
    typeof value.accepted_at === "number" &&
    (value.enqueued_at === null || typeof value.enqueued_at === "number")
  );
}

function publicPlay(record: StoredPlayEvent): Record<string, unknown> {
  return {
    event_id: record.event.event_id,
    occurred_at: record.event.occurred_at,
    game_mode: record.event.game_mode,
    rule: record.event.rule,
    chart: record.event.chart,
    play: {
      clear: record.event.play.clear,
      gauge_kind: record.event.play.gauge_kind,
      assist_kind: record.event.play.assist_kind,
      notes: record.event.play.notes,
      passnotes: record.event.play.passnotes,
      minbp: record.event.play.minbp,
      ex_score: record.event.play.ex_score,
      combo: record.event.play.combo,
      options: record.event.play.options,
    },
    accepted_at: record.accepted_at,
  };
}

function exportPlay(record: StoredPlayEvent): Record<string, unknown> {
  return {
    event: record.event,
    revision: record.revision,
    accepted_at: record.accepted_at,
  };
}

function ackFor(record: StoredPlayEvent, status: PlayAck["status"], enqueueRequired: boolean): PlayAck {
  return {
    status,
    event_id: record.event_id,
    idempotency_key: record.event_id,
    job_id: record.job_id,
    revision: record.revision,
    retryable: false,
    enqueue_required: enqueueRequired,
  };
}

function newJobId(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  const timestamp = BigInt(Math.max(0, Math.floor(Date.now())));
  for (let index = 5; index >= 0; index -= 1) {
    bytes[index] = Number(timestamp >> BigInt((5 - index) * 8) & 0xffn);
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x70;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

export class ProfileDurableObject extends DurableObject {
  private readonly state: DurableObjectState;

  constructor(state: DurableObjectState, env: Env) {
    super(state, env);
    this.state = state;
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    if (request.method === "GET" && url.pathname === "/") {
      return response({ status: "ready", component: "profile-do" });
    }
    if (request.method === "POST" && url.pathname === "/internal/play-events") {
      return this.accept(request);
    }
    if (request.method === "GET" && url.pathname === "/internal/training-summary") {
      return this.trainingSummary(url);
    }
    if (request.method === "GET" && url.pathname === "/internal/plays") {
      return this.listPlays(url);
    }
    if (request.method === "GET" && url.pathname === "/internal/privacy/export") {
      return this.exportPlays(url);
    }
    if (request.method === "POST" && url.pathname === "/internal/privacy/purge") {
      return this.purge(request);
    }
    const enqueueMatch = /^\/internal\/play-events\/([^/]+)\/enqueued$/.exec(url.pathname);
    if (request.method === "POST" && enqueueMatch) {
      return this.markEnqueued(decodeURIComponent(enqueueMatch[1]));
    }
    return response({ error: { code: "not_found" } }, 404);
  }

  private storage(): ProfileStorage {
    return this.state.storage as unknown as ProfileStorage;
  }

  private async inTransaction<T>(callback: (storage: ProfileStorage) => Promise<T>): Promise<T> {
    const storage = this.storage();
    if (typeof storage.transaction === "function") {
      return storage.transaction(callback);
    }
    return callback(storage);
  }

  private async accept(request: Request): Promise<Response> {
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return response({ error: { code: "invalid_json" } }, 400);
    }
    if (!isAcceptInput(body)) return response({ error: { code: "invalid_internal_request" } }, 400);
    const input = body;
    try {
      const result = await this.inTransaction(async (storage) => {
        const tombstone = await storage.get<unknown>("privacy:tombstone");
        if (tombstone !== undefined) throw new Error("profile_deleted");
        const identity = await storage.get<string>("profile:internal_id");
        if (identity !== undefined && identity !== input.profile_internal_id) {
          throw new Error("profile_identity_conflict");
        }
        if (identity === undefined) await storage.put("profile:internal_id", input.profile_internal_id);

        const key = `play:${input.event.event_id}`;
        const existingValue = await storage.get<unknown>(key);
        if (existingValue !== undefined) {
          if (!isStoredPlayEvent(existingValue)) throw new Error("stored_event_corrupt");
          if (
            existingValue.payload_digest !== input.payload_digest ||
            existingValue.profile_internal_id !== input.profile_internal_id ||
            existingValue.device_internal_id !== input.device_internal_id
          ) {
            return { conflict: true as const };
          }
          return {
            conflict: false as const,
            ack: ackFor(existingValue, "duplicate", existingValue.enqueued_at === null),
          };
        }

        const currentRevision = (await storage.get<number>("profile:revision")) ?? 0;
        const record: StoredPlayEvent = {
          event_id: input.event.event_id,
          payload_digest: input.payload_digest,
          profile_internal_id: input.profile_internal_id,
          device_internal_id: input.device_internal_id,
          job_id: newJobId(),
          revision: currentRevision + 1,
          accepted_at: Math.floor(Date.now() / 1000),
          event: input.event,
          enqueued_at: null,
        };
        await storage.put(key, record);
        await storage.put("profile:revision", record.revision);
        return {
          conflict: false as const,
          ack: ackFor(record, "accepted", true),
        };
      });
      if (result.conflict) return response({ error: { code: "idempotency_conflict" } }, 409);
      return response(result.ack, result.ack.status === "accepted" ? 202 : 200);
    } catch (error) {
      if (error instanceof Error && error.message === "profile_identity_conflict") {
        return response({ error: { code: "profile_conflict" } }, 409);
      }
      if (error instanceof Error && error.message === "profile_deleted") {
        return response({ error: { code: "profile_deleted" } }, 410);
      }
      return response({ error: { code: "temporary_unavailable" } }, 503);
    }
  }

  private async markEnqueued(eventId: string): Promise<Response> {
    if (!/^[0-9a-f-]{36}$/.test(eventId)) return response({ error: { code: "invalid_event_id" } }, 400);
    try {
      const marked = await this.inTransaction(async (storage) => {
        const stored = await storage.get<unknown>(`play:${eventId}`);
        if (!isStoredPlayEvent(stored)) return false;
        if (stored.enqueued_at !== null) return true;
        await storage.put(`play:${eventId}`, {
          ...stored,
          enqueued_at: Math.floor(Date.now() / 1000),
        });
        return true;
      });
      return marked ? response({ marked: true }) : response({ error: { code: "not_found" } }, 404);
    } catch {
      return response({ error: { code: "temporary_unavailable" } }, 503);
    }
  }

  private async purge(request: Request): Promise<Response> {
    let body: unknown;
    try {
      body = await request.json();
    } catch {
      return response({ error: { code: "invalid_json" } }, 400);
    }
    if (!isRecord(body) || typeof body.profile_internal_id !== "string" || typeof body.deletion_id !== "string") {
      return response({ error: { code: "invalid_internal_request" } }, 400);
    }
    try {
      const storage = this.storage();
      const tombstone = await storage.get<{ profile_internal_id: string; deletion_id: string }>("privacy:tombstone");
      if (tombstone) {
        return tombstone.profile_internal_id === body.profile_internal_id && tombstone.deletion_id === body.deletion_id
          ? response({ purged: true, duplicate: true })
          : response({ error: { code: "privacy_tombstone_conflict" } }, 409);
      }
      const identity = await storage.get<string>("profile:internal_id");
      if (identity !== undefined && identity !== body.profile_internal_id) {
        return response({ error: { code: "profile_conflict" } }, 409);
      }
      await storage.deleteAll();
      // deleteAll removes every prior value, so persist the tombstone last.
      // accept() checks it before profile identity can be recreated.
      await storage.put("privacy:tombstone", {
        profile_internal_id: body.profile_internal_id,
        deletion_id: body.deletion_id,
        deleted_at: Math.floor(Date.now() / 1000),
      });
      return response({ purged: true, duplicate: false });
    } catch {
      return response({ error: { code: "temporary_unavailable" } }, 503);
    }
  }

  private async trainingSummary(url: URL): Promise<Response> {
    const days = Number(url.searchParams.get("days") ?? "30");
    if (![7, 30, 90].includes(days)) return response({ error: { code: "invalid_days" } }, 400);
    const cutoff = Date.now() - days * 24 * 60 * 60 * 1000;
    try {
      const values = await this.storage().list<unknown>({ prefix: "play:", limit: 1000 });
      const records = [...values.values()].filter(isStoredPlayEvent).filter((item) => Date.parse(item.event.occurred_at) >= cutoff);
      const aggregate = records.reduce((result, item) => {
        result.ex_score += item.event.play.ex_score;
        result.minbp += item.event.play.minbp;
        result.notes += item.event.play.notes;
        return result;
      }, { ex_score: 0, minbp: 0, notes: 0 });
      return response({
        window_days: days,
        play_count: records.length,
        totals: aggregate,
        averages: {
          ex_score: records.length ? aggregate.ex_score / records.length : 0,
          minbp: records.length ? aggregate.minbp / records.length : 0,
        },
        data_quality: { scanned_limit: 1000, truncated: values.size === 1000 },
      });
    } catch {
      return response({ error: { code: "temporary_unavailable" } }, 503);
    }
  }

  private async listPlays(url: URL): Promise<Response> {
    const limit = Number(url.searchParams.get("limit") ?? "50");
    const cursor = url.searchParams.get("cursor") ?? undefined;
    if (!Number.isInteger(limit) || limit < 1 || limit > 100 || (cursor !== undefined && !cursor.startsWith("play:"))) {
      return response({ error: { code: "invalid_pagination" } }, 400);
    }
    try {
      const values = await this.storage().list<unknown>({ prefix: "play:", start: cursor ? `${cursor}\0` : undefined, limit: limit + 1 });
      const entries = [...values.entries()].filter((entry): entry is [string, StoredPlayEvent] => isStoredPlayEvent(entry[1]));
      const page = entries.slice(0, limit);
      return response({
        plays: page.map(([, record]) => publicPlay(record)),
        next_cursor: entries.length > limit ? page.at(-1)?.[0] ?? null : null,
      });
    } catch {
      return response({ error: { code: "temporary_unavailable" } }, 503);
    }
  }

  private async exportPlays(url: URL): Promise<Response> {
    const limit = Number(url.searchParams.get("limit") ?? "500");
    const cursor = url.searchParams.get("cursor") ?? undefined;
    if (!Number.isInteger(limit) || limit < 1 || limit > 500 || (cursor !== undefined && !cursor.startsWith("play:"))) {
      return response({ error: { code: "invalid_pagination" } }, 400);
    }
    try {
      if (await this.storage().get<unknown>("privacy:tombstone") !== undefined) {
        return response({ error: { code: "profile_deleted" } }, 410);
      }
      const values = await this.storage().list<unknown>({ prefix: "play:", start: cursor ? `${cursor}\0` : undefined, limit: limit + 1 });
      const entries = [...values.entries()].filter((entry): entry is [string, StoredPlayEvent] => isStoredPlayEvent(entry[1]));
      const page = entries.slice(0, limit);
      return response({
        plays: page.map(([, record]) => exportPlay(record)),
        next_cursor: entries.length > limit ? page.at(-1)?.[0] ?? null : null,
      });
    } catch {
      return response({ error: { code: "temporary_unavailable" } }, 503);
    }
  }
}
