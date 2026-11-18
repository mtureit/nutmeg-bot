import { expect, test } from "bun:test";
import { expandHits, parseChunkId } from "../src/retriever";
import type { Context, Env } from "../src/types";

const context = (
  id: string,
  similarity: number,
  text: string,
  title: string,
): Context => ({
  id,
  similarity,
  text,
  metadata: { title, url: `https://notion.example/${title}` },
});

const envWith = (getByIds: (ids: string[]) => Promise<unknown>): Env =>
  ({ VECTORIZE: { getByIds } } as unknown as Env);

test("parses a sequence after the last dash", () => {
  expect(parseChunkId("1234-abcd-2")).toEqual({ pageId: "1234-abcd", seq: 2 });
  expect(parseChunkId("page-0")).toEqual({ pageId: "page", seq: 0 });
  expect(parseChunkId("page-no-sequence")).toBeNull();
});

test("fetches neighbours, deduplicates hits, and restores document order", async () => {
  let requested: string[] = [];
  const env = envWith(async (ids) => {
    requested = ids;
    return [
      { id: "page-abc-1", metadata: { text: "one", title: "Page" } },
      { id: "page-abc-3", metadata: { text: "duplicate three", title: "Page" } },
      { id: "other-0", metadata: { text: "other zero", title: "Other" } },
      // page-abc-4 and other-2 are intentionally missing.
    ];
  });

  const contexts = await expandHits(
    env,
    [
      context("page-abc-2", 0.9, "two", "Page"),
      context("page-abc-3", 0.8, "three", "Page"),
      context("other-1", 0.7, "other one", "Other"),
    ],
  );

  expect(requested.sort()).toEqual(["other-0", "other-2", "page-abc-1", "page-abc-2", "page-abc-3", "page-abc-4"]);
  expect(contexts).toHaveLength(2);
  expect(contexts[0].text).toBe("# Page\n\none\n\ntwo\n\nthree");
  expect(contexts[0].text.match(/three/g)?.length).toBe(1);
  expect(contexts[1].text).toBe("# Other\n\nother zero\n\nother one");
});

test("drops lower-ranked pages when the context cap is reached", async () => {
  const env = envWith(async () => []);
  const contexts = await expandHits(env, [
    context("high-0", 0.95, "H".repeat(8_000), "High"),
    context("low-0", 0.8, "L".repeat(8_000), "Low"),
  ]);

  expect(contexts).toHaveLength(1);
  expect(contexts[0].metadata.title).toBe("High");
  expect(contexts[0].text.length).toBeLessThanOrEqual(12_000);
});

test("keeps hits without neighbours", async () => {
  const contexts = await expandHits(
    envWith(async () => []),
    [context("page-7", 0.8, "only hit", "Page")],
  );

  expect(contexts[0].text).toBe("# Page\n\nonly hit");
});
