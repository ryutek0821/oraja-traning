import { EmailMessage } from "cloudflare:email";

export type AuthEmailPurpose = "verify" | "password_reset";

export type AuthEmailInput = {
  to: string;
  purpose: AuthEmailPurpose;
  token: string;
  origin: string;
};

export type AuthEmailSender = {
  send(input: AuthEmailInput): Promise<void>;
};

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const OPAQUE_TOKEN_PATTERN = /^[A-Za-z0-9_-]{32,128}$/;

function encodeBase64Url(value: Uint8Array): string {
  let binary = "";
  for (const byte of value) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function decodeBase64Url(value: string): Uint8Array {
  if (!value || !/^[A-Za-z0-9_-]+$/.test(value) || value.length % 4 === 1) {
    throw new Error("invalid_base64url");
  }
  const normalized = value.replace(/-/g, "+").replace(/_/g, "/").padEnd(Math.ceil(value.length / 4) * 4, "=");
  const binary = atob(normalized);
  return Uint8Array.from(binary, (character) => character.charCodeAt(0));
}

function utf8(value: string): Uint8Array {
  return new TextEncoder().encode(value);
}

function arrayBuffer(value: Uint8Array): ArrayBuffer {
  return Uint8Array.from(value).buffer;
}

function assertEmail(email: string): void {
  if (email.length > 320 || !EMAIL_PATTERN.test(email)) {
    throw new Error("invalid_email");
  }
}

function assertOpaqueToken(token: string): void {
  if (!OPAQUE_TOKEN_PATTERN.test(token)) throw new Error("invalid_email_token");
}

function linkFor(input: AuthEmailInput): string {
  assertOpaqueToken(input.token);
  const origin = new URL(input.origin);
  if (
    origin.protocol !== "https:" ||
    origin.username ||
    origin.password ||
    origin.search ||
    origin.hash
  ) {
    throw new Error("email_origin_must_be_https");
  }
  const path = input.purpose === "verify" ? "/auth/verify-email" : "/auth/reset";
  const url = new URL(path, origin);
  url.searchParams.set("token", input.token);
  return url.toString();
}

/**
 * The token is an opaque, single-use capability. The message deliberately
 * contains only the opaque capability link, with no secret, recovery code,
 * account id, or internal id.
 */
export function buildAuthEmail(input: AuthEmailInput): { subject: string; text: string } {
  assertEmail(input.to);
  const link = linkFor(input);
  if (input.purpose === "verify") {
    return {
      subject: "oraja-training メールアドレスの確認",
      text: [
        "oraja-training のメールアドレス確認を受け付けました。",
        "確認する場合は次のリンクを開いてください。",
        link,
        "このリンクは短時間で失効し、一度だけ使用できます。心当たりがなければ破棄してください。",
      ].join("\n\n"),
    };
  }
  return {
    subject: "oraja-training アカウント再設定",
    text: [
      "oraja-training のアカウント再設定を受け付けました。",
      "再設定する場合は次のリンクを開いてください。",
      link,
      "このリンクは15分で失効し、一度だけ使用できます。心当たりがなければ破棄してください。",
    ].join("\n\n"),
  };
}

export class CloudflareAuthEmailSender implements AuthEmailSender {
  constructor(
    private readonly binding: SendEmail,
    private readonly from: string,
  ) {
    assertEmail(from);
  }

  async send(input: AuthEmailInput): Promise<void> {
    const message = buildAuthEmail(input);
    const raw = [
      `From: ${this.from}`,
      `To: ${input.to}`,
      `Subject: ${message.subject}`,
      "Content-Type: text/plain; charset=UTF-8",
      "Auto-Submitted: auto-generated",
      "",
      message.text,
      "",
    ].join("\r\n");
    await this.binding.send(new EmailMessage(this.from, input.to, raw));
  }
}

/** Encrypts the optional address at rest; the key is supplied as a secret. */
export async function encryptEmail(email: string, secret: string): Promise<string> {
  assertEmail(email);
  if (secret.length < 16) throw new Error("email_encryption_secret_too_short");
  const key = await encryptionKey(secret, "encrypt");
  const iv = crypto.getRandomValues(new Uint8Array(12));
  const ciphertext = new Uint8Array(
    await crypto.subtle.encrypt({ name: "AES-GCM", iv: arrayBuffer(iv) }, key, arrayBuffer(utf8(email))),
  );
  return `v1.${encodeBase64Url(iv)}.${encodeBase64Url(ciphertext)}`;
}

export async function decryptEmail(value: string, secret: string): Promise<string> {
  if (secret.length < 16) throw new Error("email_encryption_secret_too_short");
  const [version, encodedIv, encodedCiphertext] = value.split(".");
  if (version !== "v1" || !encodedIv || !encodedCiphertext) throw new Error("invalid_encrypted_email");
  const iv = decodeBase64Url(encodedIv);
  const ciphertext = decodeBase64Url(encodedCiphertext);
  if (iv.length !== 12 || ciphertext.length < 16) throw new Error("invalid_encrypted_email");
  const key = await encryptionKey(secret, "decrypt");
  const plaintext = await crypto.subtle.decrypt(
    { name: "AES-GCM", iv: arrayBuffer(iv) },
    key,
    arrayBuffer(ciphertext),
  );
  const email = new TextDecoder().decode(plaintext);
  assertEmail(email);
  return email;
}

async function encryptionKey(
  secret: string,
  usage: "encrypt" | "decrypt",
): Promise<CryptoKey> {
  const digest = await crypto.subtle.digest("SHA-256", arrayBuffer(utf8(secret)));
  return crypto.subtle.importKey("raw", digest, "AES-GCM", false, [usage]);
}
