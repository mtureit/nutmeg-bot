// Slack I/O: request signature check (from translation-ai), posting, and the
// guest check that bot.py did with users.info (PLAN.md 17.4).

// Slack signs body + timestamp with the signing secret (HMAC-SHA256).
// Requests older than 5 minutes are treated as replays.
export const verifySlackSignature = async (
  body: string,
  timestamp: string | null,
  signature: string | null,
  signingSecret: string,
): Promise<boolean> => {
  if (!timestamp || !signature || !signingSecret) return false;
  const age = Math.abs(Date.now() / 1000 - Number(timestamp));
  if (!Number.isFinite(age) || age > 60 * 5) return false;

  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(signingSecret), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(`v0:${timestamp}:${body}`));
  const expected = "v0=" + [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");

  // Constant-time compare.
  if (expected.length !== signature.length) return false;
  let diff = 0;
  for (let i = 0; i < expected.length; i++) diff |= expected.charCodeAt(i) ^ signature.charCodeAt(i);
  return diff === 0;
};

export const postMessage = async (channel: string, text: string, threadTs: string, token: string) => {
  const res = await fetch("https://slack.com/api/chat.postMessage", {
    method: "POST",
    headers: { "Content-Type": "application/json; charset=utf-8", Authorization: `Bearer ${token}` },
    body: JSON.stringify({ channel, text, thread_ts: threadTs, unfurl_links: false, unfurl_media: false }),
  });
  const data: any = await res.json().catch(() => ({}));
  if (!res.ok || !data.ok) throw new Error(`chat.postMessage failed: ${res.status} ${data.error ?? ""}`);
};

/** Refuse external guests. Fails closed: a lookup error counts as "not allowed". */
export const isAllowedUser = async (userId: string, token: string, allowGuests: boolean): Promise<boolean> => {
  if (allowGuests) return true;
  try {
    const res = await fetch(`https://slack.com/api/users.info?user=${encodeURIComponent(userId)}`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    const data: any = await res.json();
    if (!data.ok) {
      console.warn("users.info failed:", data.error);
      return false;
    }
    const user = data.user ?? {};
    return !(user.is_stranger || user.is_restricted || user.is_ultra_restricted);
  } catch (err) {
    console.warn("users.info error:", String(err));
    return false;
  }
};

export const isAllowedChannel = (channelId: string, allowed: string | undefined): boolean => {
  const list = (allowed ?? "").split(",").map((s) => s.trim()).filter(Boolean);
  return list.length === 0 || list.includes(channelId);
};

/** Remove "<@U123>" mentions, leaving the question itself. */
export const stripMentions = (text: string): string => text.replace(/<@[A-Z0-9]+>/g, "").trim();
