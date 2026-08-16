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
  list<T>(options?: { prefix?: string; start?: string; limit?: number; reverse?: boolean }): Promise<Map<string, T>>;
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

const MAX_PRIVACY_EXPORT_PAGE_BYTES = 4 * 1024 * 1024;

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

function portableState(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(portableState);
  if (!isRecord(value)) return value;
  return Object.fromEntries(Object.entries(value)
    .filter(([key]) => !["profile_internal_id", "device_internal_id"].includes(key))
    .map(([key, item]) => [key, portableState(item)]));
}

function containerPlayEvent(record: StoredPlayEvent): Record<string, unknown> {
  const score = irScore(record, record.profile_internal_id);
  return {
    contract: "container-play-event",
    schema_version: 1,
    event_id: record.event_id,
    profile_id: record.profile_internal_id,
    payload_digest: record.payload_digest,
    row: {
      sha256: score.sha256,
      mode: score.lntype,
      date: score.date,
      playcount: record.revision,
      clear: score.clear,
      notes: score.notes,
      passnotes: score.passnotes,
      minbp: score.minbp,
      maxcombo: score.maxcombo,
      option: score.option,
      seed: score.seed,
      assist: score.assist,
      gauge: score.gauge,
      epg: score.epg,
      lpg: score.lpg,
      egr: score.egr,
      lgr: score.lgr,
      egd: score.egd,
      lgd: score.lgd,
      ebd: score.ebd,
      lbd: score.lbd,
      epr: score.epr,
      lpr: score.lpr,
      ems: score.ems,
      lms: score.lms,
    },
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

type IrScoreProjection = {
  sha256: string;
  lntype: number;
  id: string;
  player: string;
  clear: number;
  date: number;
  epg: number;
  lpg: number;
  egr: number;
  lgr: number;
  egd: number;
  lgd: number;
  ebd: number;
  lbd: number;
  epr: number;
  lpr: number;
  ems: number;
  lms: number;
  avgjudge: number;
  maxcombo: number;
  notes: number;
  passnotes: number;
  minbp: number;
  option: number;
  seed: number;
  assist: number;
  gauge: number;
  device_type: null;
  judge_algorithm: null;
  rule: null;
  skin: string;
};

function encodedOption(options: string[]): number {
  const values = ["NORMAL", "MIRROR", "RANDOM", "R-RANDOM", "S-RANDOM", "H-RANDOM", "SPIRAL", "ALL-SCR", "EX-RANDOM", "EX-S-RANDOM"];
  const primary = values.indexOf(options[0] ?? "NORMAL");
  const secondary = options.length > 1 ? values.indexOf(options[1]) : -1;
  const battle = options.includes("BATTLE-ASSIST") ? 300 : options.includes("BATTLE") ? 200 : 0;
  return battle + (secondary >= 0 ? secondary * 10 : 0) + Math.max(0, primary);
}

function irScore(record: StoredPlayEvent, profilePublicId: string): IrScoreProjection {
  const play = record.event.play;
  const judgements = play.judgements;
  const gauges = ["ASSIST_EASY", "EASY", "NORMAL", "HARD", "EXHARD", "HAZARD"];
  const gauge = Math.max(0, gauges.indexOf(play.gauge_kind));
  const assist = play.assist_kind === "NONE" ? 0 : play.assist_kind === "LIGHT_ASSIST_EASY" ? 1 : 2;
  return {
    sha256: record.event.chart.sha256,
    lntype: record.event.chart.ln_mode ?? 0,
    id: profilePublicId,
    player: "",
    clear: play.clear,
    date: Math.floor(Date.parse(record.event.occurred_at) / 1000),
    epg: judgements.epg,
    lpg: judgements.lpg,
    egr: judgements.egr,
    lgr: judgements.lgr,
    egd: judgements.egd,
    lgd: judgements.lgd,
    ebd: judgements.ebd,
    lbd: judgements.lbd,
    epr: judgements.epr,
    lpr: judgements.lpr,
    ems: judgements.ems,
    lms: judgements.lms,
    avgjudge: 0,
    maxcombo: play.combo,
    notes: play.notes,
    passnotes: play.passnotes,
    minbp: play.minbp,
    option: encodedOption(play.options),
    seed: play.seed,
    assist,
    gauge,
    device_type: null,
    judge_algorithm: null,
    rule: null,
    skin: "",
  };
}

function betterIrScore(candidate: IrScoreProjection, current: IrScoreProjection): boolean {
  const candidateEx = (candidate.epg + candidate.lpg) * 2 + candidate.egr + candidate.lgr;
  const currentEx = (current.epg + current.lpg) * 2 + current.egr + current.lgr;
  return candidateEx > currentEx
    || (candidateEx === currentEx && candidate.clear > current.clear)
    || (candidateEx === currentEx && candidate.clear === current.clear && candidate.minbp < current.minbp)
    || (candidateEx === currentEx && candidate.clear === current.clear && candidate.minbp === current.minbp && candidate.date > current.date);
}

export function projectIrScores(
  values: Iterable<unknown>,
  profilePublicId: string,
  sha256?: string,
  lnMode?: number,
): IrScoreProjection[] {
  const best = new Map<string, IrScoreProjection>();
  for (const value of values) {
    if (!isStoredPlayEvent(value) || value.event.is_course) continue;
    if (sha256 !== undefined && value.event.chart.sha256 !== sha256) continue;
    const projected = irScore(value, profilePublicId);
    if (lnMode !== undefined && projected.lntype !== lnMode) continue;
    const key = `${projected.sha256}:${projected.lntype}`;
    const current = best.get(key);
    if (!current || betterIrScore(projected, current)) best.set(key, projected);
  }
  return [...best.values()].sort((left, right) => left.sha256.localeCompare(right.sha256) || left.lntype - right.lntype);
}

function exportPlay(record: StoredPlayEvent): Record<string, unknown> {
  return {
    event: record.event,
    revision: record.revision,
    accepted_at: record.accepted_at,
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
    if (request.method === "GET" && url.pathname === "/internal/ir/scores") {
      return this.irScores(url);
    }
    if (request.method === "GET" && url.pathname === "/internal/privacy/export") {
      return this.exportPlays(url);
    }
    if (request.method === "POST" && url.pathname === "/internal/privacy/purge") {
      return this.purge(request);
    }
    const readEventMatch = /^\/internal\/play-events\/([^/]+)$/.exec(url.pathname);
    if (request.method === "GET" && readEventMatch) {
      return this.readPlayEvent(decodeURIComponent(readEventMatch[1]));
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

  private async readPlayEvent(eventId: string): Promise<Response> {
    if (!/^[0-9a-f-]{36}$/.test(eventId)) return response({ error: { code: "invalid_event_id" } }, 400);
    try {
      const stored = await this.storage().get<unknown>(`play:${eventId}`);
      if (!isStoredPlayEvent(stored)) return response({ error: { code: "not_found" } }, 404);
      return response(containerPlayEvent(stored));
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

  private async irScores(url: URL): Promise<Response> {
    const profilePublicId = url.searchParams.get("profile_id") ?? "";
    const sha256 = url.searchParams.get("sha256") ?? undefined;
    const rawLnMode = url.searchParams.get("ln_mode");
    const lnMode = rawLnMode === null ? undefined : Number(rawLnMode);
    if (!/^[0-9a-f-]{36}$/.test(profilePublicId)
      || (sha256 !== undefined && !/^[0-9a-f]{64}$/.test(sha256))
      || (lnMode !== undefined && (!Number.isInteger(lnMode) || lnMode < 0 || lnMode > 2))) {
      return response({ error: { code: "invalid_ir_query" } }, 400);
    }
    try {
      if (await this.storage().get<unknown>("privacy:tombstone") !== undefined) {
        return response({ error: { code: "profile_deleted" } }, 410);
      }
      const values = await this.storage().list<unknown>({ prefix: "play:", limit: 1001, reverse: true });
      return response({
        contract: "ir-read.v1",
        scores: projectIrScores([...values.values()].slice(0, 1000), profilePublicId, sha256, lnMode),
        truncated: values.size > 1000,
        scanned_limit: 1000,
      });
    } catch {
      return response({ error: { code: "temporary_unavailable" } }, 503);
    }
  }

  private async exportPlays(url: URL): Promise<Response> {
    const limit = Number(url.searchParams.get("limit") ?? "500");
    const cursor = url.searchParams.get("cursor") ?? undefined;
    if (!Number.isInteger(limit) || limit < 1 || limit > 500 || (cursor !== undefined && cursor.length > 512)) {
      return response({ error: { code: "invalid_pagination" } }, 400);
    }
    try {
      if (await this.storage().get<unknown>("privacy:tombstone") !== undefined) {
        return response({ error: { code: "profile_deleted" } }, 410);
      }
      const values = await this.storage().list<unknown>({ start: cursor ? `${cursor}\0` : undefined, limit: limit + 1 });
      const entries = [...values.entries()].filter(([key]) => key !== "profile:internal_id" && key !== "privacy:tombstone");
      const page = entries.slice(0, limit);
      const payload = {
        entries: page.map(([key, value]) => ({
          key: key.startsWith("play:") ? `plays/${key.slice("play:".length)}` : key,
          value: isStoredPlayEvent(value) ? exportPlay(value) : portableState(value),
        })),
        next_cursor: entries.length > limit ? page.at(-1)?.[0] ?? null : null,
      };
      if (new TextEncoder().encode(JSON.stringify(payload)).byteLength > MAX_PRIVACY_EXPORT_PAGE_BYTES) {
        return response({ error: { code: "profile_export_page_too_large" } }, 413);
      }
      return response(payload);
    } catch {
      return response({ error: { code: "temporary_unavailable" } }, 503);
    }
  }
}
