import { sha256Hex } from "./auth";
import { ApiError } from "./ir-api";
import type { OAuthPrincipal } from "./oauth";

type ProposalRow = {
  id: string;
  provider_name: string;
  title: string;
  proposal_hash: string;
  payload_json: string;
  status: "pending" | "approved" | "rejected";
  created_at: number;
  decided_at: number | null;
  decision_reason: string | null;
};

function validateText(value: unknown, name: string, max: number): string {
  if (typeof value !== "string" || value.trim().length === 0 || value.length > max) {
    throw new ApiError(`invalid_${name}`, 400);
  }
  return value.trim();
}

function safePayload(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ApiError("invalid_proposal", 400);
  const serialized = JSON.stringify(value);
  if (serialized.length > 32 * 1024) throw new ApiError("proposal_too_large", 413);
  return serialized;
}

function rowToProposal(row: ProposalRow): Record<string, unknown> {
  let payload: unknown;
  try {
    payload = JSON.parse(row.payload_json);
  } catch {
    payload = { invalid: true };
  }
  return {
    id: row.id,
    provider_name: row.provider_name,
    title: row.title,
    proposal_hash: row.proposal_hash,
    payload,
    status: row.status,
    created_at: row.created_at,
    decided_at: row.decided_at,
    decision_reason: row.decision_reason,
  };
}

export async function createAdvisorProposal(
  db: D1Database,
  principal: OAuthPrincipal,
  input: { providerName: unknown; title: unknown; payload: unknown },
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  if (!principal.scopes.includes("advisor:propose")) throw new ApiError("insufficient_scope", 403);
  const providerName = validateText(input.providerName, "provider", 120);
  const title = validateText(input.title, "title", 240);
  const payloadJson = safePayload(input.payload);
  const proposalHash = await sha256Hex(`${providerName}\n${title}\n${payloadJson}`);
  const id = crypto.randomUUID();
  await db
    .prepare(
      `INSERT INTO advisor_proposals(
         id, account_id, profile_id, provider_name, title, proposal_hash,
         payload_json, status, created_at
       ) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'pending', ?8)`,
    )
    .bind(id, principal.accountId, principal.profileId, providerName, title, proposalHash, payloadJson, now)
    .run();
  return { id, provider_name: providerName, title, proposal_hash: proposalHash, status: "pending", created_at: now };
}

export async function listAdvisorProposals(
  db: D1Database,
  principal: OAuthPrincipal,
  status?: string,
): Promise<Record<string, unknown>[]> {
  if (!principal.scopes.includes("advisor:read")) throw new ApiError("insufficient_scope", 403);
  const normalized = status === undefined ? undefined : validateText(status, "status", 16);
  if (normalized !== undefined && !["pending", "approved", "rejected"].includes(normalized)) throw new ApiError("invalid_status", 400);
  const query = normalized === undefined
    ? `SELECT id, provider_name, title, proposal_hash, payload_json, status, created_at, decided_at, decision_reason
         FROM advisor_proposals WHERE account_id = ?1 AND profile_id = ?2 ORDER BY created_at DESC LIMIT 100`
    : `SELECT id, provider_name, title, proposal_hash, payload_json, status, created_at, decided_at, decision_reason
         FROM advisor_proposals WHERE account_id = ?1 AND profile_id = ?2 AND status = ?3 ORDER BY created_at DESC LIMIT 100`;
  const result = normalized === undefined
    ? await db.prepare(query).bind(principal.accountId, principal.profileId).all<ProposalRow>()
    : await db.prepare(query).bind(principal.accountId, principal.profileId, normalized).all<ProposalRow>();
  return result.results.map(rowToProposal);
}

export async function decideAdvisorProposal(
  db: D1Database,
  principal: OAuthPrincipal,
  proposalId: string,
  decision: "approved" | "rejected",
  reason: unknown,
  now = Math.floor(Date.now() / 1000),
): Promise<Record<string, unknown>> {
  if (!principal.scopes.includes("advisor:read")) throw new ApiError("insufficient_scope", 403);
  if (!/^[0-9a-f-]{36}$/.test(proposalId)) throw new ApiError("invalid_proposal", 400);
  const decisionReason = reason === undefined ? null : validateText(reason, "decision_reason", 500);
  const result = await db
    .prepare(
      `UPDATE advisor_proposals SET status = ?1, decided_at = ?2, decision_reason = ?3
        WHERE id = ?4 AND account_id = ?5 AND profile_id = ?6 AND status = 'pending'`,
    )
    .bind(decision, now, decisionReason, proposalId, principal.accountId, principal.profileId)
    .run();
  if ((result.meta?.changes ?? 0) !== 1) throw new ApiError("proposal_not_pending", 409);
  return { id: proposalId, status: decision, decided_at: now };
}
