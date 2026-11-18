// OpenAI calls over plain fetch (no SDK, keeps the bundle small).
// Failures throw: answering "not found" on an API error would claim the
// knowledge base lacks an answer it may well have.

import {
  EMBED_DIMENSIONS,
  EMBED_MODEL,
  EMBED_TIMEOUT_MS,
  GEN_MAX_TOKENS,
  GEN_MODEL,
  GEN_TEMPERATURE,
  GEN_TIMEOUT_MS,
} from "./config";

const post = async (path: string, body: unknown, apiKey: string, timeoutMs: number) => {
  const res = await fetch(`https://api.openai.com/v1/${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${apiKey}` },
    body: JSON.stringify(body),
    signal: AbortSignal.timeout(timeoutMs),
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`openai ${path} ${res.status}: ${text.slice(0, 300)}`);
  return JSON.parse(text);
};

export const embedQuery = async (text: string, apiKey: string): Promise<number[]> => {
  const data = await post(
    "embeddings",
    { model: EMBED_MODEL, input: [text], dimensions: EMBED_DIMENSIONS },
    apiKey,
    EMBED_TIMEOUT_MS,
  );
  const vector = data?.data?.[0]?.embedding;
  if (!Array.isArray(vector)) throw new Error("openai embeddings: no vector in response");
  return vector;
};

export const chat = async (system: string, user: string, apiKey: string): Promise<string> => {
  const data = await post(
    "chat/completions",
    {
      model: GEN_MODEL,
      temperature: GEN_TEMPERATURE,
      max_tokens: GEN_MAX_TOKENS,
      messages: [
        { role: "system", content: system },
        { role: "user", content: user },
      ],
    },
    apiKey,
    GEN_TIMEOUT_MS,
  );
  return String(data?.choices?.[0]?.message?.content ?? "").trim();
};
