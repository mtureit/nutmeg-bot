// Tunable constants. Kept in step with config.py on the Python side:
// the embedding model in particular MUST match the one used to build the index.

export const EMBED_MODEL = "text-embedding-3-small";
// MUST equal Python config.EMBED_DIM.
export const EMBED_DIMENSIONS = 512;
export const GEN_MODEL = "gpt-4.1-mini";
export const GEN_TEMPERATURE = 0.2;
export const GEN_MAX_TOKENS = 1024;

export const TOP_K = 5;
// Vectorize returns a cosine *similarity* (higher = closer), unlike Chroma's distance.
// Provisional; calibrate with eval_threshold.py (PLAN.md 11).
export const SIM_THRESHOLD = 0.35;
export const MAX_CONTEXT_CHARS = 12_000;

// waitUntil() keeps running for about 30 s after the response, so the two
// OpenAI calls together must finish well inside that.
export const EMBED_TIMEOUT_MS = 8_000;
export const GEN_TIMEOUT_MS = 18_000;

// The response is acknowledged before this work starts, but waitUntil is still
// bounded. Keep a small reserve for the final Slack request.
export const REQUEST_DEADLINE_MS = 27_000;
export const LIVE_FALLBACK_MIN_REMAINING_MS = 12_000;
export const LIVE_KEYWORD_TIMEOUT_MS = 4_000;
export const LIVE_SECOND_CHAT_MIN_MS = 5_000;
export const RELATED_TIMEOUT_MS = 4_000;
export const SLACK_RESPONSE_RESERVE_MS = 1_500;
export const NOTION_CACHE_TTL_SEC = 24 * 60 * 60;
export const LIVE_PAGE_MAX_CHARS = 6_000;

export const NOTION_ARTICLES_DATA_SOURCE_ID = "113f6d30-276e-42d1-b085-9c840797adf4";
export const NOTION_TAG_LIBRARY_DATA_SOURCE_ID = "ac4f4aba-5d42-4bb0-9757-1bd146a880c4";

export const EXCLUDED_NOTION_IDS = [
  "284783e8-ea0d-4059-b30a-c4493ac6efc2",
  "f64e82c8-3f8f-40c3-a43a-a27329720153",
  "27941f19-2063-8039-acca-f5c5fbea70e1",
  "27941f19-2063-804f-b861-000b2855ae14",
  "ef1f844d-6740-4d48-8025-8358d472a8a5",
  "2dd82cae-ae96-4ab1-ba01-56cc83ffef5b",
  "06e8c716-a0fb-4f79-b29d-7a4c22d7a628",
  "0451f7f2-b562-44d7-be81-5bff9da547aa",
  "35141f19-2063-80be-856c-c1fcc631c3a3",
  "35141f19-2063-8066-9762-000b1f1c95bf",
  "35141f19-2063-8077-8769-e805f7d76005",
  "35141f19-2063-80b0-90cc-000bee4b4d51",
  "25aa47a6-0726-473e-927d-8b78d69e85de",
  "396decef-74bd-4fc9-93a1-0b256ad3cae8",
] as const;

export const EXCLUDE_TITLE_KEYWORDS = ["よもやま", "雑談", "アイスブレイク", "メンバーデータベース"] as const;

// Same event re-delivered by Slack. Short is enough.
export const DEDUP_EVENT_TTL_SEC = 300;

export const NOT_FOUND_MESSAGE = "Notion に情報が見つかりませんでした";
export const ERROR_MESSAGE = "一時的にエラーが発生しました。しばらくしてからもう一度お試しください。";
export const EMPTY_QUESTION_MESSAGE = "質問を書いてメンションしてください。";
