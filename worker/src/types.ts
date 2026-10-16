// Bindings and secrets the Worker receives. Secrets are set with `wrangler secret put`.
export type Env = {
  VECTORIZE: VectorizeIndex;
  DEDUP: KVNamespace;
  SLACK_BOT_TOKEN: string;
  SLACK_SIGNING_SECRET: string;
  OPENAI_API_KEY: string;
  // Optional vars (wrangler.jsonc "vars"). Comma-separated channel ids; empty = every channel.
  ALLOWED_SLACK_CHANNELS?: string;
  // "true" lets single/multi-channel guests ask. Anything else refuses them.
  ALLOW_GUEST_USERS?: string;
};

export type Context = {
  text: string;
  metadata: Record<string, unknown>;
  similarity: number;
};
