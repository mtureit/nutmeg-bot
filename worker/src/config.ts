// Tunable constants. Kept in step with config.py on the Python side:
// the embedding model in particular MUST match the one used to build the index.

export const EMBED_MODEL = "text-embedding-3-small";
export const GEN_MODEL = "gpt-4.1-mini";
export const GEN_TEMPERATURE = 0.2;
export const GEN_MAX_TOKENS = 1024;

export const TOP_K = 5;
// Vectorize returns a cosine *similarity* (higher = closer), unlike Chroma's distance.
// Provisional; calibrate with eval_threshold.py (PLAN.md 11).
export const SIM_THRESHOLD = 0.35;

// waitUntil() keeps running for about 30 s after the response, so the two
// OpenAI calls together must finish well inside that.
export const EMBED_TIMEOUT_MS = 8_000;
export const GEN_TIMEOUT_MS = 18_000;

// Same event re-delivered by Slack. Short is enough.
export const DEDUP_EVENT_TTL_SEC = 300;

export const NOT_FOUND_MESSAGE = "Notion に情報が見つかりませんでした";
export const ERROR_MESSAGE = "一時的にエラーが発生しました。しばらくしてからもう一度お試しください。";
export const EMPTY_QUESTION_MESSAGE = "質問を書いてメンションしてください。";
