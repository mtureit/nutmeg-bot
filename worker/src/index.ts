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
  LIVE_SECOND_CHAT_MIN_MS,
  NOT_FOUND_MESSAGE,
  REQUEST_DEADLINE_MS,
  RELATED_TIMEOUT_MS,
  SLACK_RESPONSE_RESERVE_MS,
} from "./config";
import { answerJson } from "./openai";
import { handleCommand, liveFallback, normalizeNotionId, relatedPages } from "./notion";
import { buildPrompt, buildSources, SYSTEM_PROMPT } from "./prompt";
import { createDeadline, search, usableRemaining, type RequestDeadline } from "./retriever";
import { isAllowedChannel, isAllowedUser, postMessage, stripMentions, verifySlackSignature } from "./slack";
import type { Context, Env } from "./types";

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

type AnswerPath = "index" | "live-after-notfound" | "live-no-context" | "notfound";

type AnswerResult = { text: string; found: boolean };

const answered = (text: string, found: boolean, path: AnswerPath): AnswerResult => {
  console.log(`answer path=${path} found=${found}`);
  return { text, found };
};

const notFound = (): AnswerResult => answered(NOT_FOUND_MESSAGE, false, "notfound");

const pageIdsOf = (contexts: Context[]): Set<string> => new Set(
  contexts
    .map((context) => String(context.metadata.page_id ?? ""))
    .filter(Boolean)
    .map(normalizeNotionId),
);

const withoutIndexedPages = (
  contexts: Context[],
  indexedPageIds: Set<string>,
): Context[] => contexts.filter((context) => {
  const pageId = String(context.metadata.page_id ?? "");
  return !pageId || !indexedPageIds.has(normalizeNotionId(pageId));
});

export const answer = async (
  env: Env,
  question: string,
  deadline: RequestDeadline = createDeadline(REQUEST_DEADLINE_MS),
): Promise<AnswerResult> => {
  const contexts = await search(env, question, deadline);
  if (!contexts) {
    if (deadline.remaining() < LIVE_FALLBACK_MIN_REMAINING_MS) return notFound();
    const liveContexts = await liveFallback(env, question, deadline);
    if (!liveContexts.length) return notFound();
    const timeout = Math.min(GEN_TIMEOUT_MS, usableRemaining(deadline));
    if (timeout <= 0) return notFound();
    const result = await answerJson(SYSTEM_PROMPT, buildPrompt(question, liveContexts), env.OPENAI_API_KEY, timeout);
    if (result.found === false) return notFound();
    return answered(result.answer + buildSources(liveContexts), true, "live-no-context");
  }
  const timeout = Math.min(GEN_TIMEOUT_MS, usableRemaining(deadline));
  if (timeout <= 0) throw new Error("request deadline exceeded before answer");

  const indexedPageIds = pageIdsOf(contexts);
  const liveController = env.NOTION_TOKEN ? new AbortController() : null;
  const liveRemaining = Math.max(
    0,
    deadline.remaining() - SLACK_RESPONSE_RESERVE_MS - LIVE_SECOND_CHAT_MIN_MS,
  );
  const liveDeadline = createDeadline(liveRemaining);
  const livePromise = liveController
    ? liveFallback(env, question, liveDeadline, liveController.signal, indexedPageIds).catch(() => [])
    : null;
  const relatedDeadline = createDeadline(Math.min(RELATED_TIMEOUT_MS, Math.max(0, deadline.remaining() - SLACK_RESPONSE_RESERVE_MS)));
  const relatedPromise = relatedPages(env, contexts, relatedDeadline).catch(() => "");
  let result: Awaited<ReturnType<typeof answerJson>>;
  try {
    result = await answerJson(SYSTEM_PROMPT, buildPrompt(question, contexts), env.OPENAI_API_KEY, timeout);
  } catch (error) {
    liveController?.abort();
    void livePromise;
    void relatedPromise;
    throw error;
  }

  if (result.found === false) {
    void relatedPromise;
    const liveContexts = livePromise ? withoutIndexedPages(await livePromise, indexedPageIds) : [];
    if (liveContexts.length === 0 || usableRemaining(deadline) < LIVE_SECOND_CHAT_MIN_MS) return notFound();
    const secondTimeout = Math.min(GEN_TIMEOUT_MS, usableRemaining(deadline));
    if (secondTimeout < LIVE_SECOND_CHAT_MIN_MS) return notFound();
    try {
      const liveResult = await answerJson(
        SYSTEM_PROMPT,
        buildPrompt(question, liveContexts),
        env.OPENAI_API_KEY,
        secondTimeout,
      );
      if (liveResult.found === false) return notFound();
      return answered(liveResult.answer + buildSources(liveContexts), true, "live-after-notfound");
    } catch {
      return notFound();
    }
  }

  // No sources on a "not found" reply. A real index answer wins over live retrieval.
  liveController?.abort();
  void livePromise;
  const related = await relatedPromise;
  const suffix = [buildSources(contexts), related ? `\n${related}` : ""].join("");
  return answered(result.answer + suffix, true, "index");
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
    const { found, text } = await answer(env, question, deadline);
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
