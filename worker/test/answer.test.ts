import { afterEach, beforeEach, expect, test } from "bun:test";
import { answer } from "../src/index";
import { NOT_FOUND_MESSAGE } from "../src/config";
import { createDeadline, type RequestDeadline } from "../src/retriever";
import type { Env } from "../src/types";

const previousFetch = globalThis.fetch;

const makeKV = () => ({
  get: async () => null,
  put: async () => {},
}) as unknown as KVNamespace;

const makeEnv = (notion = true, matches: any[] = [
  { id: "index-page-0", score: 0.9, metadata: { text: "索引本文", title: "索引ページ", url: "https://www.notion.so/index" } },
]): Env => ({
  DEDUP: makeKV(),
  VECTORIZE: { query: async () => ({ matches }) } as unknown as VectorizeIndex,
  OPENAI_API_KEY: "openai-test",
  NOTION_TOKEN: notion ? "notion-test" : "",
  SLACK_BOT_TOKEN: "slack-test",
  SLACK_SIGNING_SECRET: "signing-test",
});

let chatReplies: string[] = [];
let keywordReply = '{"keywords":["研究"]}';
let livePageId = "live-page";
let notionCalls: string[] = [];
let delayKeyword = false;
let keywordCalls = 0;

const answerFetch = (async (url: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
  const text = String(url);
  if (text.endsWith("/embeddings")) return Response.json({ data: [{ embedding: [0.1, 0.2] }] });
  if (text.endsWith("/chat/completions")) {
    const body = JSON.parse(String(init?.body ?? "{}"));
    if (body.response_format) {
      const system = String(body.messages?.[0]?.content ?? "");
      if (system.includes("質問から Notion")) {
        keywordCalls += 1;
        if (!delayKeyword) return Response.json({ choices: [{ message: { content: keywordReply } }] });
        return await new Promise<Response>((resolve) => {
          init?.signal?.addEventListener("abort", () => resolve(Response.json({ choices: [{ message: { content: keywordReply } }] })), { once: true });
        });
      }
      return Response.json({ choices: [{ message: { content: chatReplies.shift() ?? "unexpected chat" } }] });
    }
    return Response.json({ choices: [{ message: { content: chatReplies.shift() ?? "unexpected chat" } }] });
  }
  if (text.endsWith("/search")) {
    notionCalls.push("search");
    return Response.json({ results: [{
      id: livePageId,
      url: `https://www.notion.so/${livePageId}`,
      parent: { type: "workspace" },
      properties: { Name: { type: "title", title: [{ plain_text: "研究ページ" }] } },
    }] });
  }
  if (text.includes(`/blocks/${livePageId}/children`)) {
    notionCalls.push("blocks");
    return Response.json({ results: [
      { type: "paragraph", paragraph: { rich_text: [{ plain_text: "ライブ本文" }] } },
    ], has_more: false });
  }
  throw new Error(`unexpected fetch: ${text}`);
}) as typeof fetch;

beforeEach(() => {
  globalThis.fetch = answerFetch;
});

afterEach(() => {
  globalThis.fetch = previousFetch;
  chatReplies = [];
  keywordReply = '{"keywords":["研究"]}';
  livePageId = "live-page";
  notionCalls = [];
  delayKeyword = false;
  keywordCalls = 0;
});

test("returns the index answer and cancels speculative live work", async () => {
  delayKeyword = true;
  chatReplies = ['{"found":true,"answer":"索引回答"}'];

  const result = await answer(makeEnv(), "質問");
  await new Promise((resolve) => setTimeout(resolve, 0));

  expect(result.text).toContain("索引回答");
  expect(result.text).not.toContain("索引外");
  expect(notionCalls).toEqual([]);
  expect(keywordCalls).toBe(1);
});

test("uses live pages after a JSON not-found result", async () => {
  chatReplies = [
    '{"found":false,"answer":"2024年4月のBingo MTに関する情報はNotionに見つかりませんでした。"}',
    '{"found":true,"answer":"ライブ回答"}',
  ];

  const result = await answer(makeEnv(), "質問");

  expect(result.text).toContain("ライブ回答");
  expect(result.text).toContain("（索引外・Notion から直接取得）");
  expect(notionCalls).toEqual(["search", "blocks"]);
  expect(keywordCalls).toBe(1);
});

test("drops a live page already present in the index", async () => {
  livePageId = "indexpage";
  chatReplies = ['{"found":false,"answer":""}'];

  const result = await answer(makeEnv(), "質問");

  expect(result.text).toBe(NOT_FOUND_MESSAGE);
  expect(chatReplies).toEqual([]);
});

test("does not start the second chat when little time remains", async () => {
  chatReplies = ['{"found":false,"answer":""}'];
  const shortDeadline: RequestDeadline = {
    startedAt: Date.now(),
    limitMs: 5_000,
    remaining: () => 5_000,
  };

  const result = await answer(makeEnv(), "質問", shortDeadline);

  expect(result.text).toBe(NOT_FOUND_MESSAGE);
  expect(chatReplies).toEqual([]);
  expect(notionCalls).toEqual([]);
});

test("does not call Notion without a Notion token", async () => {
  chatReplies = ['{"found":true,"answer":"索引回答"}'];

  const result = await answer(makeEnv(false), "質問");

  expect(notionCalls).toEqual([]);
});

test("treats an invalid JSON not-found paraphrase as not found", async () => {
  chatReplies = [
    "2024年4月のBingo MTに関する情報はNotionに見つかりませんでした。",
    '{"found":true,"answer":"ライブ回答"}',
  ];

  const result = await answer(makeEnv(), "質問");

  expect(result.text).toContain("ライブ回答");
  expect(result.text).toContain("（索引外・Notion から直接取得）");
});

test("treats a found result with an empty answer as not found", async () => {
  chatReplies = ['{"found":true,"answer":""}'];

  const result = await answer(makeEnv(false), "質問");

  expect(result.text).toBe(NOT_FOUND_MESSAGE);
  expect(result.found).toBe(false);
});

test("does not post a truncated JSON fragment", async () => {
  chatReplies = ['{"found":true,"answer":"途中で切れた'];

  const result = await answer(makeEnv(false), "質問");

  expect(result).toEqual({ text: NOT_FOUND_MESSAGE, found: false });
  expect(result.text).not.toContain("found");
});

test("accepts found as the string true", async () => {
  chatReplies = ['{"found":"true","answer":"文字列 true でも回答"}'];

  const result = await answer(makeEnv(false), "質問");

  expect(result.text).toContain("文字列 true でも回答");
  expect(result.found).toBe(true);
});

test("treats a short found answer with a not-found phrase as not found", async () => {
  chatReplies = [
    '{"found":true,"answer":"記載がありません"}',
    '{"found":true,"answer":"ライブ回答"}',
  ];

  const result = await answer(makeEnv(), "質問");

  expect(result.text).toContain("ライブ回答");
  expect(result.text).toContain("（索引外・Notion から直接取得）");
});

test("keeps a long partial answer with a later not-found phrase", async () => {
  const partial = `質問への回答は資料にある日付と担当者です。${"補足情報。".repeat(25)}詳細な記載がありません。`;
  chatReplies = [`{"found":true,"answer":${JSON.stringify(partial)}}`];

  const result = await answer(makeEnv(false), "質問");

  expect(result.text).toContain(partial);
  expect(result.found).toBe(true);
});

test("treats a not-found phrase in the first sentence as not found", async () => {
  const partial = `記載がありません。${"補足情報。".repeat(30)}`;
  chatReplies = [`{"found":true,"answer":${JSON.stringify(partial)}}`];

  const result = await answer(makeEnv(false), "質問");

  expect(result.text).toBe(NOT_FOUND_MESSAGE);
  expect(result.found).toBe(false);
});

test("parses fenced answer JSON", async () => {
  chatReplies = ["```json\n{\"found\":true,\"answer\":\"フェンス回答\"}\n```"];

  const result = await answer(makeEnv(false), "質問");

  expect(result.text).toContain("フェンス回答");
  expect(result.found).toBe(true);
});

test("passes through an injection report as a found answer", async () => {
  chatReplies = ['{"found":true,"answer":"締切は 2026-11-01 です。\\n※ 参考資料内に指示文が含まれていました"}'];

  const result = await answer(makeEnv(false), "質問");

  expect(result.text).toContain("締切は 2026-11-01 です。");
  expect(result.text).toContain("※ 参考資料内に指示文が含まれていました");
  expect(result.found).toBe(true);
});

test("reuses precomputed keywords and does not call the keyword model twice", async () => {
  chatReplies = [
    '{"found":false,"answer":""}',
    '{"found":true,"answer":"ライブ回答"}',
  ];

  const result = await answer(makeEnv(), "質問");

  expect(result.text).toContain("ライブ回答");
  expect(keywordCalls).toBe(1);
});

test("uses live retrieval with 10 seconds remaining but skips it with 8", async () => {
  chatReplies = [
    '{"found":false,"answer":""}',
    '{"found":true,"answer":"10秒のライブ回答"}',
  ];
  const tenSeconds: RequestDeadline = {
    startedAt: Date.now(),
    limitMs: 10_000,
    remaining: () => 10_000,
  };
  const liveResult = await answer(makeEnv(), "質問", tenSeconds);
  expect(liveResult.text).toContain("10秒のライブ回答");
  expect(notionCalls).toContain("search");

  notionCalls = [];
  keywordCalls = 0;
  chatReplies = ['{"found":false,"answer":""}'];
  const eightSeconds: RequestDeadline = {
    startedAt: Date.now(),
    limitMs: 8_000,
    remaining: () => 8_000,
  };
  const skippedResult = await answer(makeEnv(), "質問", eightSeconds);
  expect(skippedResult.text).toBe(NOT_FOUND_MESSAGE);
  expect(notionCalls).toEqual([]);
  expect(keywordCalls).toBe(1);
});

