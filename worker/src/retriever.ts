// Vectorize nearest-neighbour search with a similarity threshold and page-level
// neighbour expansion. Chunk ids are `${pageId}-${seq}`.

import { EMBED_TIMEOUT_MS, MAX_CONTEXT_CHARS, SIM_THRESHOLD, SLACK_RESPONSE_RESERVE_MS, TOP_K } from "./config";
import { embedQuery } from "./openai";
import type { Context, Env } from "./types";

export type RequestDeadline = {
  readonly startedAt: number;
  readonly limitMs: number;
  remaining(): number;
};

export const createDeadline = (limitMs: number): RequestDeadline => {
  const startedAt = Date.now();
  return {
    startedAt,
    limitMs,
    remaining: () => Math.max(0, limitMs - (Date.now() - startedAt)),
  };
};

export const usableRemaining = (deadline: RequestDeadline): number =>
  Math.max(0, deadline.remaining() - SLACK_RESPONSE_RESERVE_MS);

type ChunkId = { pageId: string; seq: number };

type RetrievedChunk = Context & {
  id: string;
  pageId: string;
  seq: number;
  isHit: boolean;
};

type PageGroup = {
  pageId: string;
  firstHitOrder: number;
  bestScore: number;
  chunks: Map<string, RetrievedChunk>;
};

/** Parse the sequence suffix while allowing dashes in a Notion page id. */
export const parseChunkId = (id: string): ChunkId | null => {
  const separator = id.lastIndexOf("-");
  if (separator <= 0 || separator === id.length - 1) return null;
  const pageId = id.slice(0, separator);
  const seqText = id.slice(separator + 1);
  if (!/^\d+$/.test(seqText)) return null;
  const seq = Number(seqText);
  if (!Number.isSafeInteger(seq)) return null;
  return { pageId, seq };
};

const metadataOf = (metadata: unknown): Record<string, unknown> =>
  metadata !== null && typeof metadata === "object"
    ? (metadata as Record<string, unknown>)
    : {};

const contextFromMatch = (match: { id: string; score: number; metadata?: unknown }): Context & { id: string } => {
  const metadata = metadataOf(match.metadata);
  return {
    id: match.id,
    text: String(metadata.text ?? ""),
    metadata,
    similarity: match.score,
  };
};

const contextFromVector = (vector: { id: string; metadata?: unknown }): Context & { id: string } => {
  const metadata = metadataOf(vector.metadata);
  return {
    id: vector.id,
    text: String(metadata.text ?? ""),
    metadata,
    // A neighbour has no query score. The containing page supplies its rank.
    similarity: 0,
  };
};

export const searchAll = async (env: Env, question: string, deadline?: RequestDeadline): Promise<Context[]> => {
  const timeoutMs = deadline ? Math.min(EMBED_TIMEOUT_MS, usableRemaining(deadline)) : EMBED_TIMEOUT_MS;
  if (timeoutMs <= 0) throw new Error("request deadline exceeded before embedding");
  const vector = await embedQuery(question, env.OPENAI_API_KEY, timeoutMs);
  const result = await env.VECTORIZE.query(vector, { topK: TOP_K, returnMetadata: "all" });
  return (result.matches ?? []).map((match) => contextFromMatch(match));
};

const pageTitle = (chunks: RetrievedChunk[]): string => {
  for (const chunk of chunks) {
    const title = String(chunk.metadata.title ?? "").trim();
    if (title) return title;
  }
  return "";
};

const imageLinks = (chunks: RetrievedChunk[]): string[] => {
  const links: string[] = [];
  for (const chunk of chunks) {
    for (const raw of String(chunk.metadata.image_links ?? "").split(",")) {
      const link = raw.trim();
      if (link && !links.includes(link)) links.push(link);
    }
  }
  return links;
};

const renderPage = (group: PageGroup): Context => {
  const chunks = [...group.chunks.values()].sort((a, b) => a.seq - b.seq || a.id.localeCompare(b.id));
  const title = pageTitle(chunks);
  const titleLine = title ? `# ${title}` : "";
  const body = chunks.map((chunk) => chunk.text.trim()).filter(Boolean);
  // Chunk 0 normally already contains the Markdown title. Add it only when a
  // hit/neighbour set starts later in the page, so every page has one title.
  const hasTitle = Boolean(titleLine && body.some((text) => text === titleLine || text.startsWith(`${titleLine}\n`)));
  const text = [hasTitle ? "" : titleLine, ...body].filter(Boolean).join("\n\n");
  const bestHit = chunks
    .filter((chunk) => chunk.isHit)
    .sort((a, b) => b.similarity - a.similarity)[0];
  const metadata: Record<string, unknown> = {
    ...(bestHit?.metadata ?? chunks[0]?.metadata ?? {}),
    page_id: group.pageId,
    ...(title ? { title } : {}),
  };
  const links = imageLinks(chunks);
  metadata.has_image = links.length > 0;
  metadata.image_links = links.join(",");
  return {
    text,
    metadata,
    similarity: group.bestScore,
  };
};

export const expandHits = async (env: Env, hits: Context[]): Promise<Context[]> => {
  const passing = hits.filter((hit) => hit.similarity >= SIM_THRESHOLD) as Array<Context & { id: string }>;
  if (passing.length === 0) return [];

  const groups = new Map<string, PageGroup>();
  const neighbourIds = new Set<string>();

  const addChunk = (context: Context & { id: string }, isHit: boolean, order: number): void => {
    const parsed = parseChunkId(context.id);
    const pageId = parsed?.pageId ?? `invalid:${context.id}`;
    const seq = parsed?.seq ?? Number.MAX_SAFE_INTEGER;
    let group = groups.get(pageId);
    if (!group) {
      group = { pageId, firstHitOrder: order, bestScore: 0, chunks: new Map() };
      groups.set(pageId, group);
    }
    const chunk: RetrievedChunk = { ...context, pageId, seq, isHit };
    const existing = group.chunks.get(context.id);
    if (!existing || (isHit && !existing.isHit)) group.chunks.set(context.id, chunk);
    if (isHit) group.bestScore = Math.max(group.bestScore, context.similarity);
  };

  passing.forEach((hit, order) => {
    addChunk(hit, true, order);
    const parsed = parseChunkId(hit.id);
    if (!parsed) return;
    if (parsed.seq > 0) neighbourIds.add(`${parsed.pageId}-${parsed.seq - 1}`);
    neighbourIds.add(`${parsed.pageId}-${parsed.seq + 1}`);
  });

  const getByIds = env.VECTORIZE.getByIds;
  if (neighbourIds.size > 0 && typeof getByIds === "function") {
    const neighbours = await getByIds.call(env.VECTORIZE, [...neighbourIds]);
    for (const vector of neighbours ?? []) {
      const parsed = parseChunkId(vector.id);
      if (!parsed || !groups.has(parsed.pageId)) continue;
      addChunk(contextFromVector(vector), false, groups.get(parsed.pageId)?.firstHitOrder ?? 0);
    }
  }

  const pages = [...groups.values()]
    .sort((a, b) => b.bestScore - a.bestScore || a.firstHitOrder - b.firstHitOrder)
    .map(renderPage);
  const selected: Context[] = [];
  let total = 0;
  for (const page of pages) {
    const separatorLength = selected.length > 0 ? 2 : 0;
    const available = MAX_CONTEXT_CHARS - total - separatorLength;
    if (available <= 0) break;
    if (page.text.length <= available) {
      selected.push(page);
      total += separatorLength + page.text.length;
      continue;
    }
    // A single page can be larger than the cap. Keep the highest-ranked page
    // within the cap; lower-ranked pages are dropped as whole pages.
    if (selected.length === 0) selected.push({ ...page, text: page.text.slice(0, available) });
    break;
  }
  return selected;
};

/** Contexts above the threshold, expanded to neighbouring chunks by page. */
export const search = async (env: Env, question: string, deadline?: RequestDeadline): Promise<Context[] | null> => {
  const hits = await searchAll(env, question, deadline);
  if (hits.length === 0 || !hits.some((hit) => hit.similarity >= SIM_THRESHOLD)) return null;
  const contexts = await expandHits(env, hits);
  return contexts.length > 0 ? contexts : null;
};
