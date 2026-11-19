// OpenAI calls over plain fetch (no SDK, keeps the bundle small).
// Failures throw: answering "not found" on an API error would claim the
// knowledge base lacks an answer it may well have.

import {
  ANSWER_MAX_TOKENS,
  EMBED_DIMENSIONS,
  EMBED_MODEL,
  EMBED_TIMEOUT_MS,
  GEN_MAX_TOKENS,
  GEN_MODEL,
  GEN_TEMPERATURE,
  GEN_TIMEOUT_MS,
  NOT_FOUND_MESSAGE,
} from "./config";

const post = async (
  path: string,
  body: unknown,
  apiKey: string,
  timeoutMs: number,
  signal?: AbortSignal,
) => {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), Math.max(1, timeoutMs));
  const abort = () => controller.abort();
  if (signal?.aborted) controller.abort();
  signal?.addEventListener("abort", abort, { once: true });
  try {
    const res = await fetch(`https://api.openai.com/v1/${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
      body: JSON.stringify(body),
      signal: controller.signal,
    });
    const text = await res.text();
    if (!res.ok) throw new Error(`openai ${path} ${res.status}: ${text.slice(0, 300)}`);
    return JSON.parse(text);
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener("abort", abort);
  }
};

export const embedQuery = async (text: string, apiKey: string, timeoutMs = EMBED_TIMEOUT_MS): Promise<number[]> => {
  const data = await post(
    "embeddings",
    { model: EMBED_MODEL, input: [text], dimensions: EMBED_DIMENSIONS },
    apiKey,
    timeoutMs,
  );
  const vector = data?.data?.[0]?.embedding;
  if (!Array.isArray(vector)) throw new Error("openai embeddings: no vector in response");
  return vector;
};

type ChatOptions = {
  json?: boolean;
  maxTokens?: number;
};

export type AnswerJson = {
  found: boolean;
  answer: string;
};

const stripJsonFence = (raw: string): string =>
  raw.replace(/^```(?:json)?\s*/i, "").replace(/\s*```$/, "").trim();

const NOT_FOUND_PHRASES = [
  "見つかりませんでした",
  "見当たりません",
  "記載がありません",
  "記録がありません",
  "含まれていません",
  "情報がありません",
] as const;

const withoutWhitespace = (text: string): string => text.replace(/\s/g, "");

const isNormalizedNotFound = (text: string): boolean => {
  const normalized = withoutWhitespace(text);
  const normalizedMessage = withoutWhitespace(NOT_FOUND_MESSAGE);
  if (!normalized || normalized === normalizedMessage) return true;

  const containsPhrase = NOT_FOUND_PHRASES.some((phrase) => normalized.includes(phrase));
  if (!containsPhrase) return false;
  if (normalized.length <= 120) return true;

  const firstSentence = withoutWhitespace(text.split(/[。\n]/, 1)[0] ?? "");
  return NOT_FOUND_PHRASES.some((phrase) => firstSentence.includes(phrase));
};

const looksLikeJson = (text: string): boolean => /^(?:[\[{\"]|true\b|false\b|null\b|-?\d)/.test(text);

export const chat = async (
  system: string,
  user: string,
  apiKey: string,
  timeoutMs = GEN_TIMEOUT_MS,
  options: ChatOptions = {},
  signal?: AbortSignal,
): Promise<string> => {
  const data = await post(
    "chat/completions",
    {
      model: GEN_MODEL,
      temperature: GEN_TEMPERATURE,
      max_tokens: options.maxTokens ?? GEN_MAX_TOKENS,
      ...(options.json ? { response_format: { type: "json_object" } } : {}),
      messages: [
        { role: "system", content: system },
        { role: "user", content: user },
      ],
    },
    apiKey,
    timeoutMs,
    signal,
  );
  return String(data?.choices?.[0]?.message?.content ?? "").trim();
};

export const chatJson = async (
  system: string,
  user: string,
  apiKey: string,
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<unknown> => {
  const raw = await chat(system, user, apiKey, timeoutMs, { json: true, maxTokens: 128 }, signal);
  const withoutFence = stripJsonFence(raw);
  return JSON.parse(withoutFence);
};

export const answerJson = async (
  system: string,
  user: string,
  apiKey: string,
  timeoutMs: number,
  signal?: AbortSignal,
): Promise<AnswerJson> => {
  const raw = await chat(system, user, apiKey, timeoutMs, { json: true, maxTokens: ANSWER_MAX_TOKENS }, signal);
  const withoutFence = stripJsonFence(raw);
  let parsed: unknown;
  try {
    parsed = JSON.parse(withoutFence);
  } catch {
    // A truncated object/array must never be posted as a text answer.
    if (looksLikeJson(withoutFence)) return { found: false, answer: "" };
    return isNormalizedNotFound(withoutFence)
      ? { found: false, answer: "" }
      : { found: Boolean(withoutFence), answer: withoutFence };
  }

  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
    return { found: false, answer: "" };
  }

  const record = parsed as Record<string, unknown>;
  const found = record.found === true || record.found === "true";
  const answer = typeof record.answer === "string" ? record.answer.trim() : "";
  if (!found || !answer || isNormalizedNotFound(answer)) return { found: false, answer: "" };
  return { found: true, answer };
};
