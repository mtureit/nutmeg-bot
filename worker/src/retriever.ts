// Vectorize nearest-neighbour search with a similarity threshold.
// Port of retriever.py; the chunk text comes back in the vector metadata.

import { SIM_THRESHOLD, TOP_K } from "./config";
import { embedQuery } from "./openai";
import type { Context, Env } from "./types";

export const searchAll = async (env: Env, question: string): Promise<Context[]> => {
  const vector = await embedQuery(question, env.OPENAI_API_KEY);
  const result = await env.VECTORIZE.query(vector, { topK: TOP_K, returnMetadata: "all" });
  return (result.matches ?? []).map((match) => {
    const metadata = (match.metadata ?? {}) as Record<string, unknown>;
    return { text: String(metadata.text ?? ""), metadata, similarity: match.score };
  });
};

/** Contexts above the threshold, or null when even the best hit is below it. */
export const search = async (env: Env, question: string): Promise<Context[] | null> => {
  const hits = await searchAll(env, question);
  if (hits.length === 0 || hits[0].similarity < SIM_THRESHOLD) return null;
  return hits.filter((hit) => hit.similarity >= SIM_THRESHOLD);
};
