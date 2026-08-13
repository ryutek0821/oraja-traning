type TableEnv = {
  CONTROL_DB: D1Database;
  ARTIFACT_BUCKET: R2Bucket;
};

function hex(bytes: Uint8Array): string {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function capabilityHash(secret: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(secret));
  return hex(new Uint8Array(digest));
}

export function effectiveMenuDate(now: Date, timezone: string): string {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezone,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    hourCycle: "h23",
  }).formatToParts(now);
  const value = Object.fromEntries(parts.map((part) => [part.type, part.value]));
  const localDate = new Date(Date.UTC(Number(value.year), Number(value.month) - 1, Number(value.day)));
  if (Number(value.hour) < 4) localDate.setUTCDate(localDate.getUTCDate() - 1);
  return localDate.toISOString().slice(0, 10);
}

function artifactObjectKey(headerKey: string, fileName: "header.json" | "score.json"): string | null {
  if (!headerKey.endsWith("/header.json")) return null;
  return `${headerKey.slice(0, -"header.json".length)}${fileName}`;
}

export async function handleCapabilityTable(
  request: Request,
  env: TableEnv,
): Promise<Response | null> {
  const match = /^\/t\/([A-Za-z0-9_-]{43})\/(recommend|today)\/(header\.json|score\.json)$/.exec(
    new URL(request.url).pathname,
  );
  if (!match) return null;
  if (request.method !== "GET" && request.method !== "HEAD") {
    return new Response(null, { status: 405, headers: { allow: "GET, HEAD" } });
  }
  const now = Math.floor(Date.now() / 1000);
  const row = await env.CONTROL_DB.prepare(
    `SELECT l.object_key, l.content_hash
       FROM capability_hashes c
       JOIN artifact_latest l
         ON l.account_id = c.account_id
        AND l.profile_id = c.profile_id
        AND l.table_kind = c.capability_kind
      WHERE c.secret_hash = ?1 AND c.capability_kind = ?2 AND c.revoked_at IS NULL
        AND (c.expires_at IS NULL OR c.expires_at > ?3)`,
  ).bind(await capabilityHash(match[1]), match[2], now).first<{
    object_key: string;
    content_hash: string;
  }>();
  if (!row) return new Response(null, { status: 404 });
  const objectKey = artifactObjectKey(row.object_key, match[3] as "header.json" | "score.json");
  if (!objectKey) return new Response(null, { status: 404 });
  const object = await env.ARTIFACT_BUCKET.get(objectKey);
  if (!object) return new Response(null, { status: 404 });
  await env.CONTROL_DB.prepare(
    "UPDATE capability_hashes SET last_used_at = ?2 WHERE secret_hash = ?1",
  ).bind(await capabilityHash(match[1]), now).run();
  const headers = new Headers({
    "cache-control": "private, no-store",
    "content-type": object.httpMetadata?.contentType ?? "application/json; charset=utf-8",
    "etag": object.httpEtag,
    "x-content-type-options": "nosniff",
  });
  return new Response(request.method === "HEAD" ? null : object.body, { headers });
}
