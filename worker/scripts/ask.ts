// Ask the bot from the terminal, without Slack.
//
// Runs the Worker's own answer() (same prompt, threshold and source list), but
// with the Vectorize binding replaced by the REST API, so it reads the real index.
// Keys come from the repo-root .env (OPENAI_API_KEY, CLOUDFLARE_*), loaded by Bun.
//
//   cd worker && bun run ask "質問"          # answer as Slack would show it
//   cd worker && bun run ask --hits "質問"   # also list the top hits and scores

import { answer } from "../src/index";
import { searchAll } from "../src/retriever";
import { SIM_THRESHOLD } from "../src/config";
import type { Env } from "../src/types";

const need = (name: string): string => {
  const value = process.env[name];
  if (!value) {
    console.error(`${name} が未設定です（リポジトリ直下の .env を確認してください）`);
    process.exit(1);
  }
  return value;
};

const restVectorize = (accountId: string, token: string, index: string): VectorizeIndex =>
  ({
    query: async (vector: number[], options: Record<string, unknown>) => {
      const res = await fetch(
        `https://api.cloudflare.com/client/v4/accounts/${accountId}/vectorize/v2/indexes/${index}/query`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify({ vector, ...options }),
        },
      );
      const body = (await res.json()) as any;
      if (!res.ok || !body.success) throw new Error(`Vectorize query failed (${res.status}): ${JSON.stringify(body.errors)}`);
      return body.result;
    },
    getByIds: async (ids: string[]) => {
      const res = await fetch(
        `https://api.cloudflare.com/client/v4/accounts/${accountId}/vectorize/v2/indexes/${index}/get_by_ids`,
        {
          method: "POST",
          headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
          body: JSON.stringify({ ids }),
        },
      );
      const body = (await res.json()) as any;
      if (!res.ok || !body.success) throw new Error(`Vectorize get_by_ids failed (${res.status}): ${JSON.stringify(body.errors)}`);
      return body.result;
    },
  }) as unknown as VectorizeIndex;

const args = process.argv.slice(2);
const showHits = args.includes("--hits");
const question = args.filter((a) => a !== "--hits").join(" ").trim();
if (!question) {
  console.error('使い方: bun run ask [--hits] "質問"');
  process.exit(1);
}

const env = {
  OPENAI_API_KEY: need("OPENAI_API_KEY"),
  VECTORIZE: restVectorize(
    need("CLOUDFLARE_ACCOUNT_ID"),
    need("CLOUDFLARE_API_TOKEN"),
    process.env.VECTORIZE_INDEX || "nutmeg-bot-512",
  ),
} as unknown as Env;

if (showHits) {
  const hits = await searchAll(env, question);
  console.log(`--- 検索結果（しきい値 ${SIM_THRESHOLD}）`);
  for (const hit of hits) {
    const mark = hit.similarity >= SIM_THRESHOLD ? "✓" : "✗";
    console.log(`${mark} ${hit.similarity.toFixed(3)}  ${String(hit.metadata.title ?? "")}`);
  }
  console.log("---");
}

const { text } = await answer(env, question);
console.log(text);
