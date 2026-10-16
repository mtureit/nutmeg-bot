// Entry tests: signature check, url_verification, dedup, and the answer path
// run through waitUntil with Slack, OpenAI and Vectorize all mocked.

import { beforeEach, expect, test } from "bun:test";
import app, { answer } from "../src/index";
import { NOT_FOUND_MESSAGE, ERROR_MESSAGE } from "../src/config";
import { buildSources } from "../src/prompt";
import { isAllowedChannel, stripMentions } from "../src/slack";

const SIGNING_SECRET = "test-signing-secret";

const makeKV = () => {
  const store = new Map<string, string>();
  return {
    get: async (k: string) => store.get(k) ?? null,
    put: async (k: string, v: string) => { store.set(k, v); },
    delete: async (k: string) => { store.delete(k); },
  } as unknown as KVNamespace;
};

// ---- external API mocks ----
let posts: any[] = [];
let lookups: string[] = [];
let userFlags: Record<string, boolean> = {};
let chatReply = "締切は 2026-11-01 です。";
let openaiFails = false;
let matches: any[] = [];

globalThis.fetch = (async (url: any, init: any) => {
  const u = String(url);
  if (u.includes("chat.postMessage")) {
    posts.push(JSON.parse(init.body));
    return Response.json({ ok: true });
  }
  if (u.includes("users.info")) {
    lookups.push(u);
    return Response.json({ ok: true, user: { ...userFlags } });
  }
  if (openaiFails) return new Response("boom", { status: 500 });
  if (u.endsWith("/embeddings")) return Response.json({ data: [{ embedding: [0.1, 0.2] }] });
  if (u.endsWith("/chat/completions")) return Response.json({ choices: [{ message: { content: chatReply } }] });
  throw new Error("unexpected fetch: " + u);
}) as any;

const makeEnv = () => ({
  DEDUP: makeKV(),
  VECTORIZE: { query: async () => ({ matches }) } as unknown as VectorizeIndex,
  SLACK_BOT_TOKEN: "xoxb-test",
  SLACK_SIGNING_SECRET: SIGNING_SECRET,
  OPENAI_API_KEY: "sk-test",
  ALLOWED_SLACK_CHANNELS: "",
  ALLOW_GUEST_USERS: "false",
});

const hit = (score: number, title = "学会", url = "https://www.notion.so/a") => ({
  id: "x", score, metadata: { text: "- 締切: 2026-11-01", title, url, has_image: false, image_links: "" },
});

const sign = async (body: string, ts: string) => {
  const enc = new TextEncoder();
  const key = await crypto.subtle.importKey(
    "raw", enc.encode(SIGNING_SECRET), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, enc.encode(`v0:${ts}:${body}`));
  return "v0=" + [...new Uint8Array(mac)].map((b) => b.toString(16).padStart(2, "0")).join("");
};

/** POST a signed Slack payload and wait for the background work to finish. */
const send = async (payload: any, env = makeEnv(), opts: { badSig?: boolean } = {}) => {
  const body = JSON.stringify(payload);
  const ts = String(Math.floor(Date.now() / 1000));
  const sig = opts.badSig ? "v0=deadbeef" : await sign(body, ts);
  const pending: Promise<unknown>[] = [];
  const ctx = { waitUntil: (p: Promise<unknown>) => pending.push(p), passThroughOnException: () => {} };
  const res = await app.request(
    "/slack/events",
    { method: "POST", body, headers: { "x-slack-request-timestamp": ts, "x-slack-signature": sig } },
    env,
    ctx as any,
  );
  await Promise.all(pending);
  return { res, text: await res.text() };
};

const mention = (eventId = "Ev1", text = "<@UBOT> SICE の締切は？") => ({
  type: "event_callback",
  event_id: eventId,
  event: { type: "app_mention", channel: "C1", user: "U1", text, ts: "111.222" },
});

beforeEach(() => {
  posts = [];
  lookups = [];
  userFlags = {};
  chatReply = "締切は 2026-11-01 です。";
  openaiFails = false;
  matches = [hit(0.8)];
});

test("rejects a bad signature", async () => {
  const { res } = await send(mention(), makeEnv(), { badSig: true });
  expect(res.status).toBe(401);
  expect(posts.length).toBe(0);
});

test("answers url_verification with the challenge", async () => {
  const { text } = await send({ type: "url_verification", challenge: "abc" });
  expect(text).toBe("abc");
});

test("answers a mention in its thread with sources", async () => {
  await send(mention());
  expect(posts.length).toBe(1);
  expect(posts[0].thread_ts).toBe("111.222");
  expect(posts[0].text).toContain("締切は 2026-11-01 です。");
  expect(posts[0].text).toContain("出典:");
  expect(posts[0].text).toContain("<https://www.notion.so/a|学会>");
});

test("same event delivered twice is answered once", async () => {
  const env = makeEnv();
  await send(mention("EvDup"), env);
  await send(mention("EvDup"), env);
  expect(posts.length).toBe(1);
});

test("below threshold says not found without calling the chat model", async () => {
  matches = [hit(0.2)];
  chatReply = "SHOULD NOT BE USED";
  await send(mention());
  expect(posts[0].text).toBe(NOT_FOUND_MESSAGE);
});

test("model's own not-found reply carries no sources", async () => {
  chatReply = NOT_FOUND_MESSAGE;
  await send(mention());
  expect(posts[0].text).toBe(NOT_FOUND_MESSAGE);
});

test("API failure gives the error message, not a false not-found", async () => {
  openaiFails = true;
  await send(mention());
  expect(posts[0].text).toBe(ERROR_MESSAGE);
});

test("guests are ignored", async () => {
  userFlags = { is_restricted: true };
  await send(mention());
  expect(posts.length).toBe(0);
});

test("empty question gets a hint", async () => {
  await send(mention("EvEmpty", "<@UBOT>"));
  expect(posts[0].text).toContain("質問を書いて");
});

test("bot messages are ignored", async () => {
  const payload = mention("EvBot");
  (payload.event as any).bot_id = "B1";
  await send(payload);
  expect(posts.length).toBe(0);
  expect(lookups.length).toBe(0);
});

test("channel allowlist", () => {
  expect(isAllowedChannel("C1", "")).toBe(true);
  expect(isAllowedChannel("C1", "C1, C2")).toBe(true);
  expect(isAllowedChannel("C3", "C1,C2")).toBe(false);
});

test("stripMentions removes user mentions", () => {
  expect(stripMentions("<@U123ABC> こんにちは <@U999>")).toBe("こんにちは");
});

test("sources are deduplicated and taken from metadata only", () => {
  const ctx = (url: string) => ({ text: "本文に https://evil.example があっても使わない", metadata: { title: "T", url }, similarity: 0.9 });
  const out = buildSources([ctx("https://www.notion.so/a"), ctx("https://www.notion.so/a")]);
  expect(out.match(/notion\.so\/a/g)?.length).toBe(1);
  expect(out).not.toContain("evil.example");
});

test("answer() keeps only hits above the threshold", async () => {
  matches = [hit(0.8, "A", "https://www.notion.so/a"), hit(0.1, "B", "https://www.notion.so/b")];
  const { text } = await answer(makeEnv() as any, "質問");
  expect(text).toContain("notion.so/a");
  expect(text).not.toContain("notion.so/b");
});
