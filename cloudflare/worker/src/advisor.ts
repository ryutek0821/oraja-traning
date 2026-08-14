import { sha256Hex } from "./auth";
import { ApiError } from "./ir-api";
import type { OAuthPrincipal } from "./oauth";

const PROPOSAL_TTL_SECONDS = 7 * 24 * 60 * 60;
const MAX_EVIDENCE_SECONDS = 90 * 24 * 60 * 60;

type ProposalStatus = "pending" | "approved" | "rejected" | "expired";
export type AdvisorOwner = { accountId: string; profileId: string; actorId: string };
type ProposalRow = {
  id: string; provider_name: string; model_name: string; title: string;
  proposal_hash: string; body_text: string; evidence_from: number; evidence_to: number;
  status: ProposalStatus; created_at: number; expires_at: number;
  decided_at: number | null; decision_reason: string | null;
};

function validateText(value: unknown, name: string, max: number): string {
  if (typeof value !== "string" || value.trim().length === 0 || value.length > max) throw new ApiError(`invalid_${name}`, 400);
  return value.trim();
}

function safeLabel(value: unknown, name: string, max: number): string {
  const label = validateText(value, name, max);
  if (/[<>\u0000-\u001f\u007f]|javascript\s*:|data\s*:/iu.test(label)) throw new ApiError("unsafe_proposal_markup", 400);
  return label;
}

function safePlainBody(value: unknown): string {
  const body = validateText(value, "body", 8_000);
  if (/<\/?[a-z][^>]*>|javascript\s*:|data\s*:\s*text\/html|!\[[^\]]*\]\([^)]*\)/iu.test(body)) {
    throw new ApiError("unsafe_proposal_markup", 400);
  }
  // A proposal is advice, never a credential transport or a conversation dump.
  if (/\b(api[_ -]?key|access[_ -]?token|refresh[_ -]?token|password|secret|authorization|cookie)\s*[:=]/iu.test(body)) {
    throw new ApiError("secret_shaped_proposal", 400);
  }
  if (/\b(full[_ -]?(conversation|transcript)|conversation[_ -]?(text|history)|chat[_ -]?(history|messages))\s*[:=]/iu.test(body)) {
    throw new ApiError("conversation_dump_not_allowed", 400);
  }
  return body;
}

function exactObject(value: unknown, allowed: readonly string[], name: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new ApiError(`invalid_${name}`, 400);
  const result = value as Record<string, unknown>;
  if (Object.keys(result).some((key) => !allowed.includes(key))) throw new ApiError(`invalid_${name}_field`, 400);
  return result;
}

function proposalPayload(value: unknown, now: number): {
  body: string; modelName: string; evidenceFrom: number; evidenceTo: number; json: string;
} {
  const payload = exactObject(value, ["body", "evidence_period", "model", "provider_self_reported"], "proposal");
  if (payload.provider_self_reported !== true) throw new ApiError("provider_self_report_required", 400);
  const evidence = exactObject(payload.evidence_period, ["from", "to"], "evidence_period");
  const evidenceFrom = Number(evidence.from);
  const evidenceTo = Number(evidence.to);
  if (!Number.isInteger(evidenceFrom) || !Number.isInteger(evidenceTo) || evidenceFrom < 0 || evidenceFrom > evidenceTo || evidenceTo > now || evidenceTo - evidenceFrom > MAX_EVIDENCE_SECONDS) {
    throw new ApiError("invalid_evidence_period", 400);
  }
  const normalized = {
    body: safePlainBody(payload.body),
    evidence_period: { from: evidenceFrom, to: evidenceTo },
    model: safeLabel(payload.model, "model", 120),
    provider_self_reported: true,
  };
  return { body: normalized.body, modelName: normalized.model, evidenceFrom, evidenceTo, json: JSON.stringify(normalized) };
}

function rowToProposal(row: ProposalRow): Record<string, unknown> {
  return {
    id: row.id, provider_name: row.provider_name, model: row.model_name, title: row.title,
    proposal_hash: row.proposal_hash, body: row.body_text,
    evidence_period: { from: row.evidence_from, to: row.evidence_to },
    provider_self_reported: true, status: row.status, created_at: row.created_at,
    expires_at: row.expires_at, decided_at: row.decided_at, decision_reason: row.decision_reason,
  };
}

async function expireAdvisorProposals(db: D1Database, owner: AdvisorOwner, now: number): Promise<void> {
  const due = await db.prepare(
    "SELECT id, proposal_hash FROM advisor_proposals WHERE account_id = ?1 AND profile_id = ?2 AND status = 'pending' AND expires_at <= ?3 LIMIT 100",
  ).bind(owner.accountId, owner.profileId, now).all<{ id: string; proposal_hash: string }>();
  if (!due.results.length) return;
  await db.batch(due.results.flatMap((row) => [
    db.prepare("UPDATE advisor_proposals SET status = 'expired', decided_at = ?1, decision_reason = 'proposal_expired', decided_by_client_id = ?2 WHERE id = ?3 AND account_id = ?4 AND profile_id = ?5 AND status = 'pending' AND expires_at <= ?1")
      .bind(now, owner.actorId, row.id, owner.accountId, owner.profileId),
    db.prepare("INSERT INTO advisor_decision_audits(id, proposal_id, account_id, profile_id, actor_client_id, decision, proposal_hash, reason_code, occurred_at) SELECT ?1, id, account_id, profile_id, ?2, 'expired', proposal_hash, 'proposal_expired', ?3 FROM advisor_proposals WHERE id = ?4 AND account_id = ?5 AND profile_id = ?6 AND status = 'expired' AND decided_at = ?3")
      .bind(crypto.randomUUID(), owner.actorId, now, row.id, owner.accountId, owner.profileId),
  ]));
}

export async function createAdvisorProposal(db: D1Database, principal: OAuthPrincipal, input: { providerName: unknown; title: unknown; payload: unknown }, now = Math.floor(Date.now() / 1000)): Promise<Record<string, unknown>> {
  if (!principal.scopes.includes("advisor:propose")) throw new ApiError("insufficient_scope", 403);
  const providerName = safeLabel(input.providerName, "provider", 120);
  const title = safeLabel(input.title, "title", 240);
  const payload = proposalPayload(input.payload, now);
  const proposalHash = await sha256Hex(`${providerName}\n${payload.modelName}\n${title}\n${payload.json}`);
  const id = crypto.randomUUID();
  const expiresAt = now + PROPOSAL_TTL_SECONDS;
  await db.batch([
    db.prepare(`INSERT INTO advisor_proposals(id, account_id, profile_id, provider_name, model_name, provider_self_reported, title, body_text, evidence_from, evidence_to, proposal_hash, payload_json, status, created_at, expires_at, scope_snapshot)
      VALUES (?1, ?2, ?3, ?4, ?5, 1, ?6, ?7, ?8, ?9, ?10, ?11, 'pending', ?12, ?13, ?14)`)
      .bind(id, principal.accountId, principal.profileId, providerName, payload.modelName, title, payload.body, payload.evidenceFrom, payload.evidenceTo, proposalHash, payload.json, now, expiresAt, [...principal.scopes].sort().join(" ")),
    db.prepare(`INSERT INTO audit_events(id, account_id, profile_id, actor_kind, event_type, resource_hash, status, occurred_at)
      VALUES (?1, ?2, ?3, 'oauth', 'advisor.proposal.created', ?4, 'pending', ?5)`)
      .bind(crypto.randomUUID(), principal.accountId, principal.profileId, proposalHash, now),
  ]);
  return { id, provider_name: providerName, model: payload.modelName, title, proposal_hash: proposalHash, status: "pending", created_at: now, expires_at: expiresAt };
}

export async function listAdvisorProposals(db: D1Database, owner: AdvisorOwner, status?: string, now = Math.floor(Date.now() / 1000)): Promise<Record<string, unknown>[]> {
  await expireAdvisorProposals(db, owner, now);
  const normalized = status === undefined ? undefined : validateText(status, "status", 16);
  if (normalized !== undefined && !["pending", "approved", "rejected", "expired"].includes(normalized)) throw new ApiError("invalid_status", 400);
  const columns = "id, provider_name, model_name, title, proposal_hash, body_text, evidence_from, evidence_to, status, created_at, expires_at, decided_at, decision_reason";
  const result = normalized === undefined
    ? await db.prepare(`SELECT ${columns} FROM advisor_proposals WHERE account_id = ?1 AND profile_id = ?2 ORDER BY created_at DESC LIMIT 100`).bind(owner.accountId, owner.profileId).all<ProposalRow>()
    : await db.prepare(`SELECT ${columns} FROM advisor_proposals WHERE account_id = ?1 AND profile_id = ?2 AND status = ?3 ORDER BY created_at DESC LIMIT 100`).bind(owner.accountId, owner.profileId, normalized).all<ProposalRow>();
  return result.results.map(rowToProposal);
}

export async function decideAdvisorProposal(db: D1Database, owner: AdvisorOwner, proposalId: string, decision: "approved" | "rejected", reason: unknown, now = Math.floor(Date.now() / 1000)): Promise<Record<string, unknown>> {
  if (!/^[0-9a-f-]{36}$/.test(proposalId)) throw new ApiError("invalid_proposal", 400);
  await expireAdvisorProposals(db, owner, now);
  const decisionReason = reason === undefined ? null : validateText(reason, "decision_reason", 500);
  const decisionMarker = crypto.randomUUID();
  const update = db.prepare(`UPDATE advisor_proposals SET status = ?1, decided_at = ?2, decision_reason = ?3, decided_by_client_id = ?4, decision_marker = ?5
    WHERE id = ?6 AND account_id = ?7 AND profile_id = ?8 AND status = 'pending' AND expires_at > ?2`)
    .bind(decision, now, decisionReason, owner.actorId, decisionMarker, proposalId, owner.accountId, owner.profileId);
  const statements: D1PreparedStatement[] = [update];
  if (decision === "approved") statements.push(db.prepare(`INSERT INTO advisor_journal(id, proposal_id, account_id, profile_id, provider_name, model_name, title, body_text, evidence_from, evidence_to, proposal_hash, proposal_created_at, approved_at, approved_by_client_id, scope_snapshot)
    SELECT ?1, id, account_id, profile_id, provider_name, model_name, title, body_text, evidence_from, evidence_to, proposal_hash, created_at, ?2, ?3, scope_snapshot
      FROM advisor_proposals WHERE id = ?4 AND account_id = ?5 AND profile_id = ?6 AND status = 'approved' AND decision_marker = ?7`)
    .bind(crypto.randomUUID(), now, owner.actorId, proposalId, owner.accountId, owner.profileId, decisionMarker));
  statements.push(
    db.prepare(`INSERT INTO advisor_decision_audits(id, proposal_id, account_id, profile_id, actor_client_id, decision, proposal_hash, reason_code, occurred_at)
      SELECT ?1, id, account_id, profile_id, ?2, ?3, proposal_hash, ?4, ?5 FROM advisor_proposals
       WHERE id = ?6 AND account_id = ?7 AND profile_id = ?8 AND status = ?3 AND decision_marker = ?9`)
      .bind(crypto.randomUUID(), owner.actorId, decision, decisionReason, now, proposalId, owner.accountId, owner.profileId, decisionMarker),
    db.prepare(`INSERT INTO audit_events(id, account_id, profile_id, actor_kind, event_type, reason_code, resource_hash, status, occurred_at)
      SELECT ?1, account_id, profile_id, 'oauth', ?2, ?3, proposal_hash, ?4, ?5 FROM advisor_proposals
       WHERE id = ?6 AND account_id = ?7 AND profile_id = ?8 AND status = ?4 AND decision_marker = ?9`)
      .bind(crypto.randomUUID(), `advisor.proposal.${decision}`, decisionReason, decision, now, proposalId, owner.accountId, owner.profileId, decisionMarker),
  );
  const results = await db.batch(statements);
  if ((results[0].meta?.changes ?? 0) !== 1) throw new ApiError("proposal_not_pending", 409);
  return { id: proposalId, status: decision, decided_at: now };
}
