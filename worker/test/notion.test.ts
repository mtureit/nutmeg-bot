import { afterEach, expect, test } from "bun:test";
import {
  blocksToMarkdown,
  handleCommand,
  liveFallback,
  parseCommand,
  relatedPages,
} from "../src/notion";
import { createDeadline } from "../src/retriever";
import { NOTION_ARTICLES_DATA_SOURCE_ID, NOTION_TAG_LIBRARY_DATA_SOURCE_ID } from "../src/config";
import { buildSources } from "../src/prompt";
import type { Env } from "../src/types";

const previousFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = previousFetch;
});

const makeKV = (initial: Record<string, unknown> = {}) => {
  const values = new Map(Object.entries(initial).map(([key, value]) => [key, JSON.stringify(value)]));
  return {
    get: async (key: string) => values.get(key) ?? null,
    put: async (key: string, value: string) => { values.set(key, value); },
  } as unknown as KVNamespace;
};

const envWith = (token = "notion-test-token", initial: Record<string, unknown> = {}): Env => ({
  DEDUP: makeKV(initial),
  OPENAI_API_KEY: "openai-test",
  NOTION_TOKEN: token,
  VECTORIZE: {} as VectorizeIndex,
  SLACK_BOT_TOKEN: "slack-test",
  SLACK_SIGNING_SECRET: "signing-test",
});

const tagPage = (id: string, name: string) => ({
  id,
  properties: { Name: { type: "title", title: [{ plain_text: name }] } },
});

const articlePage = (id: string, title: string, date: string, tags: string[] = []) => ({
  id,
  url: `https://www.notion.so/${id}`,
  properties: {
    Name: { type: "title", title: [{ plain_text: title }] },
    Date: { type: "date", date: { start: date } },
    Tag: { type: "relation", relation: tags.map((tag) => ({ id: tag })) },
  },
});

test("parses commands without involving an LLM", () => {
  expect(parseCommand("タグ一覧")).toEqual({ kind: "tag-list" });
  expect(parseCommand("タグ インフラ")).toEqual({ kind: "tag", name: "インフラ" });
  expect(parseCommand("最近")).toEqual({ kind: "recent" });
  expect(parseCommand("help")).toEqual({ kind: "help" });
  expect(parseCommand("これは質問")).toEqual({ kind: "question" });
});

test("matches tag names exactly, by a unique partial, or reports ambiguity", async () => {
  const fetchImpl = async (url: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const text = String(url);
    if (text.includes(NOTION_TAG_LIBRARY_DATA_SOURCE_ID)) {
      return Response.json({ results: [tagPage("tag-infra", "インフラ"), tagPage("tag-infra-old", "インフラ運用")], has_more: false });
    }
    if (text.includes(NOTION_ARTICLES_DATA_SOURCE_ID)) {
      const body = JSON.parse(String(init?.body ?? "{}"));
      const tagId = body.filter?.relation?.contains;
      return Response.json({
        results: tagId ? [articlePage("article-1", "運用手順", "2026-11-18", [tagId])] : [],
        has_more: false,
      });
    }
    throw new Error(`unexpected URL ${text}`);
  };
  globalThis.fetch = fetchImpl;
  const env = envWith();

  const exact = await handleCommand(env, "タグ インフラ", createDeadline(2_000));
  expect(exact).toContain("運用手順");
  const partial = await handleCommand(env, "タグ 運用", createDeadline(2_000));
  expect(partial).toContain("運用手順");
  const ambiguous = await handleCommand(env, "タグ フラ", createDeadline(2_000));
  expect(ambiguous).toContain("一意に決まりません");
  const unknown = await handleCommand(env, "タグ 研究", createDeadline(2_000));
  expect(unknown).toContain("タグ一覧");
});

test("lists tag counts from the cache and excludes article ids", async () => {
  const env = envWith("notion-test-token", {
    "cache:tagcounts": [
      { id: "tag-infra", name: "インフラ", count: 228 },
      { id: "tag-docs", name: "文書", count: 5 },
    ],
  });
  const answer = await handleCommand(env, "タグ一覧", createDeadline(2_000));
  expect(answer).toContain("インフラ (228)");
  expect(answer).toContain("文書 (5)");
});

test("converts supported blocks, truncates, and redacts live content", () => {
  const blocks = [
    { type: "heading_1", heading_1: { rich_text: [{ plain_text: "見出し" }] } },
    { type: "paragraph", paragraph: { rich_text: [{ plain_text: "password: dummy-pass-123" }] } },
    { type: "bulleted_list_item", bulleted_list_item: { rich_text: [{ plain_text: "項目" }] } },
    { type: "to_do", to_do: { checked: true, rich_text: [{ plain_text: "完了" }] } },
    { type: "code", code: { language: "ts", rich_text: [{ plain_text: "const x = 1" }] } },
    { type: "quote", quote: { rich_text: [{ plain_text: "引用" }] } },
    { type: "callout", callout: { rich_text: [{ plain_text: "注意" }] } },
    { type: "toggle", toggle: { rich_text: [{ plain_text: "詳細" }] } },
    { type: "table_row", table_row: { cells: [[{ plain_text: "A" }], [{ plain_text: "B" }]] } },
  ];
  const markdown = blocksToMarkdown(blocks, 6000);
  expect(markdown).toContain("# 見出し");
  expect(markdown).toContain("- [x] 完了");
  expect(markdown).toContain("```ts");
  expect(markdown).toContain("| A | B |");
  expect(markdown).toContain("password: dummy-pass-123");
  expect(blocksToMarkdown([{ type: "paragraph", paragraph: { rich_text: [{ plain_text: "x".repeat(100) }] } }], 10)).toHaveLength(10);
});

test("live fallback fails closed for excluded ancestors and retrieval errors", async () => {
  globalThis.fetch = async (url: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const text = String(url);
    if (text.endsWith("/chat/completions")) return Response.json({ choices: [{ message: { content: '{"keywords":["研究"]}' } }] });
    if (text.endsWith("/search")) {
      return Response.json({ results: [{ id: "page-live", url: "https://www.notion.so/live", parent: { type: "page_id", page_id: "parent-live" }, properties: { Name: { type: "title", title: [{ plain_text: "研究" }] } } }] });
    }
    if (text.endsWith("/pages/parent-live")) {
      return Response.json({ id: "parent-live", parent: { type: "database_id", database_id: "284783e8-ea0d-4059-b30a-c4493ac6efc2" } });
    }
    throw new Error(`unexpected URL ${text}`);
  };
  expect(await liveFallback(envWith(), "研究の質問", createDeadline(27_000))).toEqual([]);

  globalThis.fetch = async (url: RequestInfo | URL): Promise<Response> => {
    const text = String(url);
    if (text.endsWith("/chat/completions")) return Response.json({ choices: [{ message: { content: '{"keywords":["研究"]}' } }] });
    if (text.endsWith("/search")) return Response.json({ results: [{ id: "page-live", parent: { type: "page_id", page_id: "broken" }, properties: { Name: { type: "title", title: [{ plain_text: "研究" }] } } }] });
    return new Response("failed", { status: 500 });
  };
  expect(await liveFallback(envWith(), "研究の質問", createDeadline(27_000))).toEqual([]);
});

test("live fallback fetches shallow blocks, redacts them, and marks the source", async () => {
  globalThis.fetch = async (url: RequestInfo | URL): Promise<Response> => {
    const text = String(url);
    if (text.endsWith("/chat/completions")) return Response.json({ choices: [{ message: { content: '{"keywords":["研究"]}' } }] });
    if (text.endsWith("/search")) return Response.json({ results: [{ id: "page-live", url: "https://www.notion.so/live", parent: { type: "workspace" }, properties: { Name: { type: "title", title: [{ plain_text: "研究" }] } } }] });
    if (text.includes("/blocks/page-live/children")) return Response.json({ results: [
      { id: "block-1", type: "paragraph", paragraph: { rich_text: [{ plain_text: "password: dummy-pass-123" }] } },
    ], has_more: false });
    throw new Error(`unexpected URL ${text}`);
  };
  const contexts = await liveFallback(envWith(), "研究の質問", createDeadline(27_000));
  expect(contexts).toHaveLength(1);
  expect(contexts[0].text).toContain("password: [伏字]");
  expect(buildSources(contexts)).toContain("（索引外・Notion から直接取得）");
});

test("related pages use the least-used cited tag and exclude cited pages", async () => {
  const env = envWith("notion-test-token", {
    "cache:tagcounts": [
      { id: "tag-infra", name: "インフラ", count: 228 },
      { id: "tag-research", name: "研究", count: 2 },
    ],
    "cache:taglib": [
      { id: "tag-infra", name: "インフラ" },
      { id: "tag-research", name: "研究" },
    ],
  });
  globalThis.fetch = async (url: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const text = String(url);
    if (text.includes(NOTION_ARTICLES_DATA_SOURCE_ID)) {
      const body = JSON.parse(String(init?.body ?? "{}"));
      expect(body.filter.relation.contains).toBe("tag-research");
      return Response.json({ results: [articlePage("page-cited", "既出", "2026-11-18"), articlePage("page-related", "関連ページ", "2026-11-17")], has_more: false });
    }
    throw new Error(`unexpected URL ${text}`);
  };
  const result = await relatedPages(env, [{
    text: "index",
    similarity: 0.9,
    metadata: { page_id: "page-cited", tags: "インフラ, 研究", source: "index" },
  }], createDeadline(4_000));
  expect(result).toContain("関連（タグ: 研究）:");
  expect(result).toContain("関連ページ");
  expect(result).not.toContain("既出");
});

test("commands and live fallback are graceful without a Notion token or time", async () => {
  const env = envWith("");
  expect(await handleCommand(env, "タグ一覧", createDeadline(2_000))).toContain("利用できません");
  expect(await liveFallback(env, "質問", createDeadline(27_000))).toEqual([]);
  expect(await liveFallback(envWith(), "質問", createDeadline(1))).toEqual([]);
});
