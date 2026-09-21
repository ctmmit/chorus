import { describe, expect, it } from "vitest";

import { cacheReadSharePercent, episodeInputLabel, orderedStages } from "./usage";
import type { LLMTokens } from "./api-types";

function tokens(overrides: Partial<LLMTokens> = {}): LLMTokens {
  return {
    calls: 0,
    input_tokens: 0,
    output_tokens: 0,
    cache_read_tokens: 0,
    cache_write_tokens: 0,
    ...overrides,
  };
}

describe("orderedStages", () => {
  it("orders known stages ingest -> curate -> script -> audio regardless of input order", () => {
    const stages = orderedStages({ audio: 9.6, ingest: 0.8, script: 3.1, curate: 12.4 });
    expect(stages).toEqual([
      ["ingest", 0.8],
      ["curate", 12.4],
      ["script", 3.1],
      ["audio", 9.6],
    ]);
  });

  it("appends unrecognized stage keys after the known ones", () => {
    const stages = orderedStages({ ingest: 0.1, warmup: 0.2 });
    expect(stages).toEqual([
      ["ingest", 0.1],
      ["warmup", 0.2],
    ]);
  });

  it("handles a subset of known stages", () => {
    expect(orderedStages({ curate: 5 })).toEqual([["curate", 5]]);
  });
});

describe("cacheReadSharePercent", () => {
  it("computes cache_read / (input + cache_read) as a percentage", () => {
    expect(
      cacheReadSharePercent(tokens({ input_tokens: 100, cache_read_tokens: 300 })),
    ).toBeCloseTo(75, 5);
  });

  it("is null when input and cache_read are both zero", () => {
    expect(cacheReadSharePercent(tokens())).toBeNull();
  });

  it("is 0 when there is input but no cache reads yet (first call)", () => {
    expect(cacheReadSharePercent(tokens({ input_tokens: 500 }))).toBe(0);
  });
});

describe("episodeInputLabel", () => {
  it("prefers video_id", () => {
    expect(episodeInputLabel({ video_id: "abc", title: "Title" })).toBe("abc");
  });

  it("falls back to title, then guid, then feed_url, then url", () => {
    expect(episodeInputLabel({ title: "Title" })).toBe("Title");
    expect(episodeInputLabel({ guid: "guid-1" })).toBe("guid-1");
    expect(episodeInputLabel({ feed_url: "https://feed.example/rss" })).toBe(
      "https://feed.example/rss",
    );
    expect(episodeInputLabel({ url: "https://youtu.be/abc" })).toBe("https://youtu.be/abc");
  });

  it("falls back to a placeholder when nothing identifies the episode", () => {
    expect(episodeInputLabel({})).toBe("unknown episode");
  });
});
