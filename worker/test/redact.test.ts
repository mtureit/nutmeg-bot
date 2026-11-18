import { expect, test } from "bun:test";
import cases from "../../tests/fixtures/redact_cases.json";
import { redact } from "../src/redact";

test("matches the shared Python redaction fixture", () => {
  for (const item of cases as Array<{ name: string; input: string; expected: string; count: number }>) {
    const actual = redact(item.input);
    expect(actual.text).toBe(item.expected, item.name);
    expect(actual.count).toBe(item.count, item.name);
  }
});
