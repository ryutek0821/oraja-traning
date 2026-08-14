import { readCookie } from "./auth";
import { ApiError, listDevices, sessionAccount } from "./ir-api";
import type { Env } from "./index";

type ProfileRow = { id: string; public_id: string; display_name: string; timezone: string; status: string };

async function doSummary(env: Env, profileId: string, days: number): Promise<Record<string, unknown>> {
  const stub = env.PROFILE_DO.get(env.PROFILE_DO.idFromName(profileId));
  const response = await stub.fetch(`https://profile.internal/internal/training-summary?days=${days}`);
  if (!response.ok) return { play_count: 0, data_quality: { unavailable: true } };
  return response.json<Record<string, unknown>>();
}

export async function dashboardData(request: Request, env: Env): Promise<Record<string, unknown>> {
  if (request.method !== "GET") throw new ApiError("method_not_allowed", 405);
  const sessionToken = readCookie(request, "__Host-oraja_session");
  if (!sessionToken) throw new ApiError("unauthorized", 401);
  const accountId = await sessionAccount(env.CONTROL_DB, sessionToken);
  const profile = await env.CONTROL_DB.prepare(
    "SELECT id, public_id, display_name, timezone, status FROM profiles WHERE account_id = ?1 AND status <> 'deleted' ORDER BY created_at LIMIT 1",
  ).bind(accountId).first<ProfileRow>();
  if (!profile) throw new ApiError("profile_not_found", 404);
  const [devices, jobs, artifacts, deletion, summary7, summary30, summary90] = await Promise.all([
    listDevices(env.CONTROL_DB, accountId, profile.public_id),
    env.CONTROL_DB.prepare(
      "SELECT id AS job_id, kind, status, revision, updated_at, terminal_reason FROM jobs WHERE account_id = ?1 AND profile_id = ?2 ORDER BY updated_at DESC LIMIT 20",
    ).bind(accountId, profile.id).all<Record<string, unknown>>(),
    env.CONTROL_DB.prepare(
      "SELECT table_kind, revision, content_hash, updated_at FROM artifact_latest WHERE account_id = ?1 AND profile_id = ?2 ORDER BY table_kind",
    ).bind(accountId, profile.id).all<Record<string, unknown>>(),
    env.CONTROL_DB.prepare(
      "SELECT status, requested_at, cancel_until, cancelled_at, confirmed_at FROM deletion_requests WHERE account_id = ?1 AND profile_id = ?2 ORDER BY requested_at DESC LIMIT 1",
    ).bind(accountId, profile.id).first<Record<string, unknown>>(),
    doSummary(env, profile.id, 7),
    doSummary(env, profile.id, 30),
    doSummary(env, profile.id, 90),
  ]);
  return {
    profile: { public_id: profile.public_id, display_name: profile.display_name, timezone: profile.timezone, status: profile.status },
    devices,
    jobs: jobs.results,
    artifacts: artifacts.results,
    summaries: { "7": summary7, "30": summary30, "90": summary90 },
    privacy: { deletion: deletion ?? null },
  };
}
