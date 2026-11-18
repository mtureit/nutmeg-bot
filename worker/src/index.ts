// Worker entry: Slack Events API (HTTP) instead of Socket Mode.
//
// Flow: signature check -> dedup (KV) -> 200 at once -> inside waitUntil:
//   access checks -> embed question -> Vectorize search -> OpenAI answer -> reply in thread.
// Slack re-sends an event that is not acknowledged within 3 s, so the answer work
// must not happen before the response.

import { Hono } from "hono";
import {
  DEDUP_EVENT_TTL_SEC,
  EMPTY_QUESTION_MESSAGE,
  ERROR_MESSAGE,
  GEN_TIMEOUT_MS,
  LIVE_FALLBACK_MIN_REMAINING_MS,
  NOT_FOUND_MESSAGE,
  REQUEST_DEADLINE_MS,
  RELATED_TIMEOUT_MS,
  SLACK_RESPONSE_RESERVE_MS,
} from "./config";
import { chat } from "./openai";
import { handleCommand, liveFallback, relatedPages } from "./notion";
import { buildPrompt, buildSources, SYSTEM_PROMPT } from "./prompt";
import { createDeadline, search, usableRemaining, type RequestDeadline } from "./retriever";
import { isAllowedChannel, isAllowedUser, postMessage, stripMentions, verifySlackSignature } from "./slack";
import type { Env } from "./types";

/** True when the key was already recorded; records it otherwise. Best effort (KV is eventually consistent). */
const seen = async (kv: KVNamespace, key: string, ttl: number): Promise<boolean> => {
  try {
    if (await kv.get(key)) return true;
    await kv.put(key, "1", { expirationTtl: ttl });
    return false;
  } catch (err) {
    console.error("dedup failed:", String(err));
    return false;
  }
};

export const answer = async (
  env: Env,
  question: string,
  deadline: RequestDeadline = createDeadline(REQUEST_DEADLINE_MS),
): Promise<{ text: string; found: boolean }> => {
  const contexts = await search(env, question, deadline);
  if (!contexts) {
    if (deadline.remaining() < LIVE_FALLBACK_MIN_REMAINING_MS) return { text: NOT_FOUND_MESSAGE, found: false };
    const liveContexts = await liveFallback(env, question, deadline);
    if (!liveContexts.length) return { text: NOT_FOUND_MESSAGE, found: false };
    const timeout = Math.min(GEN_TIMEOUT_MS, usableRemaining(deadline));
    if (timeout <= 0) return { text: NOT_FOUND_MESSAGE, found: false };
    const reply = await chat(SYSTEM_PROMPT, buildPrompt(question, liveContexts), env.OPENAI_API_KEY, timeout);
    if (!reply || reply.includes(NOT_FOUND_MESSAGE)) return { text: NOT_FOUND_MESSAGE, found: false };
    return { text: reply + buildSources(liveContexts), found: true };
  }
  const timeout = Math.min(GEN_TIMEOUT_MS, usableRemaining(deadline));
  if (timeout <= 0) throw new Error("request deadline exceeded before answer");
  const relatedDeadline = createDeadline(Math.min(RELATED_TIMEOUT_MS, Math.max(0, deadline.remaining() - SLACK_RESPONSE_RESERVE_MS)));
  const [reply, related] = await Promise.all([
    chat(SYSTEM_PROMPT, buildPrompt(question, contexts), env.OPENAI_API_KEY, timeout),
    relatedPages(env, contexts, relatedDeadline).catch(() => ""),
  ]);
  // No sources on a "not found" reply.
  if (!reply || reply.includes(NOT_FOUND_MESSAGE)) return { text: NOT_FOUND_MESSAGE, found: false };
  const suffix = [buildSources(contexts), related ? `\n${related}` : ""].join("");
  return { text: reply + suffix, found: true };
};

export const handleMention = async (env: Env, event: any): Promise<void> => {
  const channel = String(event.channel ?? "");
  const user = String(event.user ?? "");
  const threadTs = String(event.thread_ts ?? event.ts ?? "");
  const deadline = createDeadline(REQUEST_DEADLINE_MS);

  if (!isAllowedChannel(channel, env.ALLOWED_SLACK_CHANNELS)) {
    console.log("channel not allowed:", channel);
    return;
  }
  if (!(await isAllowedUser(user, env.SLACK_BOT_TOKEN, env.ALLOW_GUEST_USERS === "true"))) {
    console.log("user not allowed:", user);
    return;
  }

  const question = stripMentions(String(event.text ?? ""));
  try {
    if (!question) {
      await postMessage(channel, EMPTY_QUESTION_MESSAGE, threadTs, env.SLACK_BOT_TOKEN);
      return;
    }
    const commandReply = await handleCommand(env, question, deadline);
    if (commandReply !== null) {
      await postMessage(channel, commandReply, threadTs, env.SLACK_BOT_TOKEN);
      return;
    }
    const { text, found } = await answer(env, question, deadline);
    // Identifiers and outcome only; the question text is not logged (PLAN.md 17.6).
    console.log(`answered user=${user} channel=${channel} found=${found}`);
    await postMessage(channel, text, threadTs, env.SLACK_BOT_TOKEN);
  } catch (err) {
    console.error("failed to answer:", (err as any)?.stack || String(err));
    try {
      await postMessage(channel, ERROR_MESSAGE, threadTs, env.SLACK_BOT_TOKEN);
    } catch (postErr) {
      console.error("failed to post error message:", String(postErr));
    }
  }
};

const app = new Hono<{ Bindings: Env }>();

app.get("/health", (c) => c.json({ status: "ok", service: "nutmeg-bot" }));

app.post("/slack/events", async (c) => {
  const raw = await c.req.text();
  const ok = await verifySlackSignature(
    raw,
    c.req.header("x-slack-request-timestamp") ?? null,
    c.req.header("x-slack-signature") ?? null,
    c.env.SLACK_SIGNING_SECRET,
  );
  if (!ok) return c.text("invalid signature", 401);

  let body: any;
  try {
    body = JSON.parse(raw);
  } catch {
    return c.text("bad request", 400);
  }

  if (body.type === "url_verification") return c.text(String(body.challenge ?? ""));
  if (body.type !== "event_callback") return c.text("OK");

  const event = body.event ?? {};
  // Only human mentions. A bot (including this one) mentioning the app is ignored.
  if (event.type !== "app_mention" || event.bot_id || event.subtype) return c.text("OK");

  if (await seen(c.env.DEDUP, `ev:${body.event_id}`, DEDUP_EVENT_TTL_SEC)) {
    console.log("dedup hit:", body.event_id);
    return c.text("OK");
  }

  c.executionCtx.waitUntil(handleMention(c.env, event));
  return c.text("OK");
});

export default app;
