import { ApiError } from "./ir-api";

export const EXPORT_CIPHER = "AES-256-GCM";

function base64Url(bytes: Uint8Array): string {
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function decodeBase64Url(value: string, expectedLength: number): Uint8Array {
  if (!/^[A-Za-z0-9_-]+$/.test(value)) throw new ApiError("invalid_export_key", 503);
  const padded = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  let binary: string;
  try { binary = atob(padded); } catch { throw new ApiError("invalid_export_key", 503); }
  const bytes = Uint8Array.from(binary, (character) => character.charCodeAt(0));
  if (bytes.byteLength !== expectedLength) throw new ApiError("invalid_export_key", 503);
  return bytes;
}

function aad(exportId: string): Uint8Array {
  return new TextEncoder().encode(`oraja.profile-export.v2\0${exportId}`);
}

function buffer(bytes: Uint8Array): ArrayBuffer {
  return bytes.slice().buffer as ArrayBuffer;
}

async function aesKey(raw: Uint8Array, usage: KeyUsage[]): Promise<CryptoKey> {
  return crypto.subtle.importKey("raw", buffer(raw), { name: "AES-GCM", length: 256 }, false, usage);
}

export type EncryptedExport = {
  ciphertext: Uint8Array;
  contentIv: string;
  wrappedKey: string;
  wrapIv: string;
  userKey: string;
};

export function exportKek(value: string | undefined): Uint8Array {
  if (!value) throw new ApiError("export_kek_not_configured", 503);
  return decodeBase64Url(value, 32);
}

export async function encryptExport(plaintext: Uint8Array, exportId: string, kekValue: string | undefined): Promise<EncryptedExport> {
  const dataKey = crypto.getRandomValues(new Uint8Array(32));
  const contentIv = crypto.getRandomValues(new Uint8Array(12));
  const wrapIv = crypto.getRandomValues(new Uint8Array(12));
  const additionalData = aad(exportId);
  const ciphertext = new Uint8Array(await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: buffer(contentIv), additionalData: buffer(additionalData), tagLength: 128 },
    await aesKey(dataKey, ["encrypt"]),
    buffer(plaintext),
  ));
  const wrappedKey = new Uint8Array(await crypto.subtle.encrypt(
    { name: "AES-GCM", iv: buffer(wrapIv), additionalData: buffer(additionalData), tagLength: 128 },
    await aesKey(exportKek(kekValue), ["encrypt"]),
    buffer(dataKey),
  ));
  return {
    ciphertext,
    contentIv: base64Url(contentIv),
    wrappedKey: base64Url(wrappedKey),
    wrapIv: base64Url(wrapIv),
    userKey: base64Url(dataKey),
  };
}

export async function unwrapExportKey(
  wrappedKey: string,
  wrapIv: string,
  exportId: string,
  kekValue: string | undefined,
): Promise<string> {
  try {
    const plaintext = await crypto.subtle.decrypt(
      { name: "AES-GCM", iv: buffer(decodeBase64Url(wrapIv, 12)), additionalData: buffer(aad(exportId)), tagLength: 128 },
      await aesKey(exportKek(kekValue), ["decrypt"]),
      buffer(decodeBase64Url(wrappedKey, 48)),
    );
    return base64Url(new Uint8Array(plaintext));
  } catch (error) {
    if (error instanceof ApiError) throw error;
    throw new ApiError("export_key_unwrap_failed", 503);
  }
}
