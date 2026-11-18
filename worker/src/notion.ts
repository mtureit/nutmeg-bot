import {
  EXCLUDED_NOTION_IDS,
  EXCLUDE_TITLE_KEYWORDS,
  LIVE_FALLBACK_MIN_REMAINING_MS,
  LIVE_KEYWORD_TIMEOUT_MS,
  LIVE_PAGE_MAX_CHARS,
  NOTION_ARTICLES_DATA_SOURCE_ID,
  NOTION_CACHE_TTL_SEC,
  NOTION_TAG_LIBRARY_DATA_SOURCE_ID,
  SLACK_RESPONSE_RESERVE_MS,
} from "./config";
import { chatJson } from "./openai";
import { redact } from "./redact";
import type { Context, Env } from "./types";
import type { RequestDeadline } from "./retriever";

const NOTION_API = "https://api.notion.com/v1";
const NOTION_VERSION = "2025-09-03";
const MAX_QUERY_PAGES = 100;

type JsonObject = Record<string, unknown>;

const objectOf = (value: unknown): JsonObject =>
  value !== null && typeof value === "object" ? (value as JsonObject) : {};

const arrayOf = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

const stringOf = (value: unknown): string => (typeof value === "string" ? value : "");

export const normalizeNotionId = (value: string): string => value.replaceAll("-", "").toLowerCase();

const excludedIds = new Set(EXCLUDED_NOTION_IDS.map(normalizeNotionId));

export const isExcludedNotionId = (value: string): boolean => excludedIds.has(normalizeNotionId(value));

export type Tag = { id: string; name: string };
export type TagCount = Tag & { count: number };
export type ArticleRow = { id: string; title: string; url: string; date: string; raw: JsonObject };

const sleep = (ms: number): Promise<void> => new Promise((resolve) => setTimeout(resolve, ms));

export class NotionClient {
  private readonly token: string;

  constructor(token: string) {
    this.token = token;
  }

  async request(
    path: string,
    deadline: RequestDeadline,
    init: RequestInit = {},
    retried429 = false,
  ): Promise<JsonObject | null> {
    const remaining = Math.max(0, deadline.remaining() - SLACK_RESPONSE_RESERVE_MS);
    if (remaining <= 0) return null;
    const headers = new Headers(init.headers);
    headers.set("Authorization", `Bearer ${this.token}`);
    headers.set("Notion-Version", NOTION_VERSION);
    headers.set("Content-Type", "application/json");
    try {
      const response = await fetch(`${NOTION_API}${path}`, {
        ...init,
        headers,
        signal: AbortSignal.timeout(Math.max(1, remaining)),
      });
      if (response.status === 429 && !retried429) {
        const retryAfter = Number(response.headers.get("Retry-After"));
        const waitMs = Number.isFinite(retryAfter) && retryAfter >= 0 ? retryAfter * 1000 : NaN;
        if (Number.isFinite(waitMs) && waitMs < Math.max(0, deadline.remaining() - SLACK_RESPONSE_RESERVE_MS)) {
          await sleep(waitMs);
          return this.request(path, deadline, init, true);
        }
        return null;
      }
      if (!response.ok) return null;
      const body: unknown = await response.json();
      return objectOf(body);
    } catch {
      return null;
    }
  }
}

const cacheGet = async (env: Env, key: string): Promise<unknown | null> => {
  if (!env.DEDUP) return null;
  try {
    const raw = await env.DEDUP.get(key);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
};

const cachePut = async (env: Env, key: string, value: unknown): Promise<void> => {
  if (!env.DEDUP) return;
  try {
    await env.DEDUP.put(key, JSON.stringify(value), { expirationTtl: NOTION_CACHE_TTL_SEC });
  } catch {
    // Cache failures must not make Notion features fail.
  }
};

const richText = (value: unknown): string => {
  return arrayOf(value).map((part) => {
    const item = objectOf(part);
    const plain = stringOf(item.plain_text);
    if (plain) return plain;
    const text = objectOf(item.text);
    return stringOf(text.content);
  }).join("");
};

const propertyValue = (page: JsonObject, name: string): JsonObject => {
  const properties = objectOf(page.properties);
  return objectOf(properties[name]);
};

export const pageTitle = (page: JsonObject): string => {
  const properties = objectOf(page.properties);
  for (const property of Object.values(properties)) {
    const item = objectOf(property);
    if (item.type === "title") {
      const title = richText(item.title);
      if (title) return title;
    }
  }
  const direct = richText(page.title);
  return direct || stringOf(page.name);
};

const pageDate = (page: JsonObject): string => {
  const property = propertyValue(page, "Date");
  const date = objectOf(property.date);
  return stringOf(date.start).slice(0, 10);
};

export const articleFromPage = (page: JsonObject): ArticleRow => ({
  id: stringOf(page.id),
  title: pageTitle(page) || "（無題）",
  url: stringOf(page.url),
  date: pageDate(page),
  raw: page,
});

const relationIds = (page: JsonObject): string[] => {
  const relation = propertyValue(page, "Tag").relation;
  return arrayOf(relation).map((item) => stringOf(objectOf(item).id)).filter(Boolean);
};

type QueryResult = { rows: JsonObject[]; complete: boolean };

const queryDataSource = async (
  client: NotionClient,
  dataSourceId: string,
  deadline: RequestDeadline,
  body: JsonObject = {},
  maxRows = Number.POSITIVE_INFINITY,
): Promise<QueryResult> => {
  const rows: JsonObject[] = [];
  let cursor: string | undefined;
  for (let page = 0; page < MAX_QUERY_PAGES && rows.length < maxRows; page++) {
    if (deadline.remaining() <= 0) return { rows, complete: false };
    const payload: JsonObject = {
      page_size: Math.min(100, Math.max(1, maxRows - rows.length)),
      ...body,
      ...(cursor ? { start_cursor: cursor } : {}),
    };
    const response = await client.request(
      `/data_sources/${encodeURIComponent(dataSourceId)}/query`,
      deadline,
      { method: "POST", body: JSON.stringify(payload) },
    );
    if (!response) return { rows, complete: false };
    rows.push(...arrayOf(response.results).map(objectOf).slice(0, maxRows - rows.length));
    if (rows.length >= maxRows) return { rows, complete: true };
    if (response.has_more !== true || !stringOf(response.next_cursor)) return { rows, complete: true };
    cursor = stringOf(response.next_cursor);
  }
  return { rows, complete: false };
};

const tagFromPage = (page: JsonObject): Tag | null => {
  const id = stringOf(page.id);
  const name = pageTitle(page).trim();
  return id && name ? { id, name } : null;
};

export const getTagLibrary = async (
  env: Env,
  client: NotionClient,
  deadline: RequestDeadline,
): Promise<{ tags: Tag[]; complete: boolean }> => {
  const cached = await cacheGet(env, "cache:taglib");
  if (Array.isArray(cached)) {
    const tags = cached.map(objectOf).map((item) => ({ id: stringOf(item.id), name: stringOf(item.name) })).filter((tag) => tag.id && tag.name);
    return { tags, complete: true };
  }
  const result = await queryDataSource(client, NOTION_TAG_LIBRARY_DATA_SOURCE_ID, deadline);
  const tags = result.rows.map(tagFromPage).filter((tag): tag is Tag => tag !== null);
  if (result.complete) await cachePut(env, "cache:taglib", tags);
  return { tags, complete: result.complete };
};

const readCachedTagCounts = async (env: Env): Promise<TagCount[] | null> => {
  const cached = await cacheGet(env, "cache:tagcounts");
  if (!Array.isArray(cached)) return null;
  const counts = cached.map(objectOf).map((item) => ({
    id: stringOf(item.id),
    name: stringOf(item.name),
    count: Number(item.count),
  })).filter((tag) => tag.id && tag.name && Number.isFinite(tag.count));
  return counts;
};

export const getTagCounts = async (
  env: Env,
  client: NotionClient,
  deadline: RequestDeadline,
): Promise<{ tags: TagCount[]; complete: boolean }> => {
  const cached = await readCachedTagCounts(env);
  if (cached) return { tags: cached, complete: true };

  const [library, articles] = await Promise.all([
    getTagLibrary(env, client, deadline),
    queryDataSource(client, NOTION_ARTICLES_DATA_SOURCE_ID, deadline),
  ]);
  if (!library.complete || !articles.complete) return { tags: library.tags.map((tag) => ({ ...tag, count: 0 })), complete: false };

  const countById = new Map<string, number>();
  for (const article of articles.rows) {
    if (isExcludedNotionId(stringOf(article.id))) continue;
    for (const id of relationIds(article)) countById.set(normalizeNotionId(id), (countById.get(normalizeNotionId(id)) ?? 0) + 1);
  }
  const tags = library.tags.map((tag) => ({ ...tag, count: countById.get(normalizeNotionId(tag.id)) ?? 0 }));
  await cachePut(env, "cache:tagcounts", tags);
  return { tags, complete: true };
};

const articleSorts = [
  { property: "Date", direction: "descending" },
  { timestamp: "last_edited_time", direction: "descending" },
];

export const queryArticles = async (
  env: Env,
  client: NotionClient,
  deadline: RequestDeadline,
  tagId?: string,
): Promise<{ rows: ArticleRow[]; complete: boolean }> => {
  const body: JsonObject = { sorts: articleSorts };
  if (tagId) body.filter = { property: "Tag", relation: { contains: tagId } };
  const result = await queryDataSource(client, NOTION_ARTICLES_DATA_SOURCE_ID, deadline, body, 10);
  return {
    rows: result.rows.filter((row) => !isExcludedNotionId(stringOf(row.id))).map(articleFromPage),
    complete: result.complete,
  };
};

export type ParsedCommand =
  | { kind: "help" }
  | { kind: "tag-list" }
  | { kind: "tag"; name: string }
  | { kind: "recent" }
  | { kind: "question" };

export const parseCommand = (question: string): ParsedCommand => {
  const text = question.trim();
  if (text === "タグ一覧") return { kind: "tag-list" };
  if (text === "最近") return { kind: "recent" };
  if (text === "ヘルプ" || text.toLowerCase() === "help") return { kind: "help" };
  const tag = /^タグ\s+(.+)$/.exec(text);
  if (tag) return { kind: "tag", name: tag[1].trim() };
  return { kind: "question" };
};

export const HELP_MESSAGE = [
  "使い方:",
  "• タグ一覧 — タグを記事数順で表示",
  "• タグ <name> — そのタグの記事を表示",
  "• 最近 — 最近編集された記事を表示",
  "• ヘルプ / help — この説明",
  "• 上記以外 — Notion の内容を検索して回答",
].join("\n");

const unavailableMessage = "Notion のコマンドは現在利用できません（NOTION_TOKEN 未設定）。";

const formatArticle = (article: ArticleRow): string => {
  const link = article.url ? `<${article.url}|${article.title}>` : article.title;
  return `• ${link}（${article.date || "日付なし"}）`;
};

const findTag = (tags: Tag[], name: string): { tag?: Tag; candidates: Tag[] } => {
  const normalized = name.trim().toLowerCase();
  const exact = tags.filter((tag) => tag.name.toLowerCase() === normalized);
  if (exact.length === 1) return { tag: exact[0], candidates: [] };
  if (exact.length > 1) return { candidates: exact };
  const partial = tags.filter((tag) => tag.name.toLowerCase().includes(normalized));
  return partial.length === 1 ? { tag: partial[0], candidates: [] } : { candidates: partial };
};

const formatCandidates = (candidates: Tag[]): string =>
  candidates.length ? `候補: ${candidates.map((tag) => tag.name).join("、")}` : "候補はありません。タグ一覧を確認してください。";

export const handleCommand = async (
  env: Env,
  question: string,
  deadline: RequestDeadline,
): Promise<string | null> => {
  const command = parseCommand(question);
  if (command.kind === "question") return null;
  if (command.kind === "help") return HELP_MESSAGE;
  if (!env.NOTION_TOKEN) return unavailableMessage;
  const client = new NotionClient(env.NOTION_TOKEN);

  if (command.kind === "tag-list") {
    const result = await getTagCounts(env, client, deadline);
    const tags = result.tags.slice().sort((a, b) => b.count - a.count || a.name.localeCompare(b.name)).slice(0, 30);
    if (!result.complete) {
      const names = tags.map((tag) => `• ${tag.name}`).join("\n");
      return `タグ一覧（件数は取得できません）:\n${names || "（タグなし）"}`;
    }
    return `タグ一覧:\n${tags.map((tag) => `• ${tag.name} (${tag.count})`).join("\n") || "（タグなし）"}`;
  }

  if (command.kind === "recent") {
    const result = await queryArticles(env, client, deadline);
    return `最近:\n${result.rows.map(formatArticle).join("\n") || "（記事が見つかりません）"}`;
  }

  const library = await getTagLibrary(env, client, deadline);
  const match = findTag(library.tags, command.name);
  if (!match.tag) {
    return match.candidates.length
      ? `タグ名が一意に決まりません。${formatCandidates(match.candidates)}`
      : `タグ「${command.name}」は見つかりません。タグ一覧を確認してください。`;
  }
  const result = await queryArticles(env, client, deadline, match.tag.id);
  return `タグ: ${match.tag.name}\n${result.rows.map(formatArticle).join("\n") || "（記事が見つかりません）"}`;
};

const parentOf = (value: JsonObject): JsonObject => objectOf(value.parent);

type ParentRef = { type: "page" | "block" | "database" | "data_source"; id: string } | { type: "workspace" } | null;

const parentRef = (parent: JsonObject): ParentRef => {
  const type = stringOf(parent.type);
  if (type === "workspace") return { type: "workspace" };
  if (type === "page_id") return { type: "page", id: stringOf(parent.page_id) };
  if (type === "block_id") return { type: "block", id: stringOf(parent.block_id) };
  if (type === "database_id") return { type: "database", id: stringOf(parent.database_id) };
  if (type === "data_source_id") return { type: "data_source", id: stringOf(parent.data_source_id) };
  return null;
};

const liveTitleExcluded = (title: string): boolean => EXCLUDE_TITLE_KEYWORDS.some((keyword) => title.includes(keyword));

const retrieveParent = async (
  client: NotionClient,
  ref: Exclude<NonNullable<ParentRef>, { type: "workspace" }>,
  deadline: RequestDeadline,
  cache: Map<string, Promise<JsonObject | null>>,
): Promise<JsonObject | null> => {
  const key = `${ref.type}:${normalizeNotionId(ref.id)}`;
  const existing = cache.get(key);
  if (existing) return existing;
  const path = ref.type === "page"
    ? `/pages/${encodeURIComponent(ref.id)}`
    : ref.type === "block"
      ? `/blocks/${encodeURIComponent(ref.id)}`
      : ref.type === "database"
        ? `/databases/${encodeURIComponent(ref.id)}`
        : `/data_sources/${encodeURIComponent(ref.id)}`;
  const request = client.request(path, deadline);
  cache.set(key, request);
  return request;
};

const allowedLivePage = async (
  page: JsonObject,
  client: NotionClient,
  deadline: RequestDeadline,
  cache: Map<string, Promise<JsonObject | null>>,
): Promise<boolean> => {
  if (isExcludedNotionId(stringOf(page.id)) || liveTitleExcluded(pageTitle(page))) return false;
  let current = page;
  for (let level = 0; level < 3; level++) {
    const ref = parentRef(parentOf(current));
    if (!ref || ref.type === "workspace") return true;
    if (!ref.id) return false;
    if (isExcludedNotionId(ref.id)) return false;
    const ancestor = await retrieveParent(client, ref, deadline, cache);
    if (!ancestor) return false;
    current = ancestor;
  }
  return true;
};

const blockText = (value: JsonObject): string => richText(value.rich_text ?? value.text);

export const blockToMarkdown = (block: JsonObject): string[] => {
  const type = stringOf(block.type);
  const data = objectOf(block[type]);
  const text = blockText(data);
  switch (type) {
    case "paragraph": return text ? [text] : [];
    case "heading_1": return text ? [`# ${text}`] : [];
    case "heading_2": return text ? [`## ${text}`] : [];
    case "heading_3": return text ? [`### ${text}`] : [];
    case "bulleted_list_item": return text ? [`- ${text}`] : [];
    case "numbered_list_item": return text ? [`1. ${text}`] : [];
    case "to_do": return text ? [`- [${data.checked === true ? "x" : " "}] ${text}`] : [];
    case "code": return text ? [`\`\`\`${stringOf(data.language) || ""}\n${text}\n\`\`\``] : [];
    case "quote": return text ? [`> ${text}`] : [];
    case "callout": return text ? [`> ${text}`] : [];
    case "toggle": return text ? [`> ${text}`] : [];
    case "table_row": {
      const cells = arrayOf(data.cells).map(richText);
      return cells.length ? [`| ${cells.join(" | ")} |`] : [];
    }
    default: return [];
  }
};

export const blocksToMarkdown = (blocks: JsonObject[], maxChars = Number.POSITIVE_INFINITY): string => {
  const lines: string[] = [];
  const visit = (block: JsonObject): void => {
    lines.push(...blockToMarkdown(block));
    for (const child of arrayOf(block.children).map(objectOf)) visit(child);
  };
  for (const block of blocks) visit(block);
  return lines.join("\n\n").slice(0, maxChars);
};

const fetchBlocks = async (
  client: NotionClient,
  blockId: string,
  depth: number,
  deadline: RequestDeadline,
): Promise<JsonObject[] | null> => {
  const blocks: JsonObject[] = [];
  let cursor: string | undefined;
  while (blocks.length < 100) {
    if (deadline.remaining() <= 0) return null;
    const query = cursor ? `?page_size=100&start_cursor=${encodeURIComponent(cursor)}` : "?page_size=100";
    const response = await client.request(`/blocks/${encodeURIComponent(blockId)}/children${query}`, deadline);
    if (!response) return null;
    const page = arrayOf(response.results).map(objectOf);
    for (const block of page) {
      if (blocks.length >= 100) break;
      blocks.push(block);
      if (depth < 2 && block.has_children === true) {
        const children = await fetchBlocks(client, stringOf(block.id), depth + 1, deadline);
        if (!children) return null;
        block.children = children;
      }
    }
    if (response.has_more !== true || !stringOf(response.next_cursor)) break;
    cursor = stringOf(response.next_cursor);
  }
  return blocks;
};

const pageMarkdown = async (
  client: NotionClient,
  page: JsonObject,
  deadline: RequestDeadline,
): Promise<string | null> => {
  const blocks = await fetchBlocks(client, stringOf(page.id), 0, deadline);
  if (!blocks) return null;
  const title = redact(pageTitle(page)).text;
  const body = blocksToMarkdown(blocks, LIVE_PAGE_MAX_CHARS);
  const redacted = redact(body).text;
  return [`# ${title}`.trim(), redacted].filter(Boolean).join("\n\n").slice(0, LIVE_PAGE_MAX_CHARS);
};

const keywordsFor = async (env: Env, question: string, deadline: RequestDeadline): Promise<string[]> => {
  const timeout = Math.min(LIVE_KEYWORD_TIMEOUT_MS, Math.max(0, deadline.remaining() - SLACK_RESPONSE_RESERVE_MS));
  if (timeout <= 0) return [];
  const result = await chatJson(
    "質問から Notion ページのタイトルに含まれそうな検索キーワードを 1〜3 個選び、JSON {\"keywords\":[\"...\"]} だけを返してください。名詞を優先してください。",
    question,
    env.OPENAI_API_KEY,
    timeout,
  );
  const raw = Array.isArray(result) ? result : arrayOf(objectOf(result).keywords);
  return raw.map(stringOf).map((keyword) => keyword.trim()).filter(Boolean).slice(0, 3);
};

export const liveFallback = async (
  env: Env,
  question: string,
  deadline: RequestDeadline,
): Promise<Context[]> => {
  if (!env.NOTION_TOKEN || deadline.remaining() < LIVE_FALLBACK_MIN_REMAINING_MS) return [];
  const client = new NotionClient(env.NOTION_TOKEN);
  let keywords: string[];
  try {
    keywords = await keywordsFor(env, question, deadline);
  } catch {
    return [];
  }
  if (!keywords.length) return [];

  const pages = new Map<string, JsonObject>();
  for (const keyword of keywords) {
    if (deadline.remaining() <= 0) break;
    const response = await client.request("/search", deadline, {
      method: "POST",
      body: JSON.stringify({ query: keyword, filter: { property: "object", value: "page" }, page_size: 5 }),
    });
    for (const page of arrayOf(response?.results).map(objectOf)) {
      const id = stringOf(page.id);
      if (id && !pages.has(id)) pages.set(id, page);
    }
  }

  const ancestorCache = new Map<string, Promise<JsonObject | null>>();
  const allowed: JsonObject[] = [];
  for (const page of pages.values()) {
    if (deadline.remaining() <= 0 || allowed.length >= 2) break;
    if (await allowedLivePage(page, client, deadline, ancestorCache)) allowed.push(page);
  }

  const contexts: Context[] = [];
  for (const page of allowed) {
    if (deadline.remaining() <= 0) break;
    const text = await pageMarkdown(client, page, deadline);
    if (text === null) continue;
    const safeTitle = redact(pageTitle(page)).text;
    contexts.push({
      text,
      metadata: {
        title: safeTitle,
        url: stringOf(page.url),
        source: "live",
        tags: "",
      },
      similarity: 0,
    });
  }
  return contexts;
};

export const relatedPages = async (
  env: Env,
  contexts: Context[],
  deadline: RequestDeadline,
): Promise<string> => {
  if (!env.NOTION_TOKEN || !contexts.length) return "";
  const sourceIds = new Set(
    contexts
      .map((context) => stringOf(context.metadata.page_id ?? context.metadata.id))
      .filter(Boolean)
      .map(normalizeNotionId),
  );
  const tags = String(contexts[0].metadata.tags ?? "").split(",").map((tag) => tag.trim()).filter(Boolean);
  if (!tags.length || deadline.remaining() <= 0) return "";
  const client = new NotionClient(env.NOTION_TOKEN);
  const cachedCounts = await readCachedTagCounts(env);
  let selected = tags[0];
  if (cachedCounts) {
    selected = tags.slice().sort((a, b) => {
      const countA = cachedCounts.find((tag) => tag.name.toLowerCase() === a.toLowerCase())?.count ?? Number.POSITIVE_INFINITY;
      const countB = cachedCounts.find((tag) => tag.name.toLowerCase() === b.toLowerCase())?.count ?? Number.POSITIVE_INFINITY;
      return countA - countB;
    })[0] ?? selected;
  }
  const library = await getTagLibrary(env, client, deadline);
  const tag = findTag(library.tags, selected).tag;
  if (!tag) return "";
  const result = await queryArticles(env, client, deadline, tag.id);
  if (!result.complete) return "";
  const rows = result.rows.filter((row) => !sourceIds.has(normalizeNotionId(row.id))).slice(0, 3);
  if (!rows.length) return "";
  return [`関連（タグ: ${tag.name}）:`, ...rows.map(formatArticle)].join("\n");
};
