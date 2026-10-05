import { describe, expect, it } from "vitest";

import type { Digest, Thread } from "./api-types";
import { chapterRows, formatDay, isDisagreement, threadRows } from "./threads";

const digest: Digest = {
  soul_version: "v",
  soul_origin: "supplied",
  episodes: [
    {
      episode_id: "aaaaaaaaaaa",
      episode_title: "Margins",
      refused: false,
      refusal_reason: null,
      duration_seconds: 600,
      windows: [],
      highlights: [
        {
          episode_id: "aaaaaaaaaaa",
          episode_title: "Margins",
          segment_timestamp: 754,
          quote: "Margins expand.",
          relevance_score: 0.9,
          why_surface: "w",
          highlight_id: "h1",
        },
      ],
    },
  ],
};

const thread: Thread = {
  question: "Do margins expand?",
  members: [
    { highlight_id: "h1", episode_id: "aaaaaaaaaaa", stance: "adds" },
    {
      highlight_id: "old",
      episode_id: "bbbbbbbbbbb",
      stance: "disagrees",
      remembered_at: "2026-09-14T08:00:00Z",
      quote: "Margins will compress.",
      source: "Skeptic Show",
    },
    { highlight_id: "missing", episode_id: "ccccccccccc", stance: "agrees" },
  ],
};

describe("threadRows", () => {
  it("resolves this week's members against the digest", () => {
    const [row] = threadRows(thread, digest);
    expect(row).toMatchObject({
      stance: "adds",
      source: "Margins",
      quote: "Margins expand.",
      clock: "12:34",
      remembered: null,
    });
    expect(row.href).toContain("t=754");
  });

  it("shows a remembered claim with its own quote and date", () => {
    const row = threadRows(thread, digest)[1];
    expect(row).toMatchObject({
      stance: "disagrees",
      source: "Skeptic Show",
      quote: "Margins will compress.",
      clock: null,
      href: null,
      remembered: "14 Sep 2026",
    });
  });

  it("drops a member it cannot show honestly", () => {
    expect(threadRows(thread, digest).map((r) => r.key)).toEqual(["h1", "old"]);
  });
});

describe("helpers", () => {
  it("flags disagreement", () => {
    expect(isDisagreement(thread)).toBe(true);
    expect(isDisagreement({ question: "q", members: [thread.members[0]] })).toBe(false);
  });

  it("formats days in the house style", () => {
    expect(formatDay("2026-10-04T23:59:00Z")).toBe("04 Oct 2026");
    expect(formatDay("not a date")).toBe("not a date");
  });

  it("builds chapter rows", () => {
    expect(chapterRows(undefined)).toEqual([]);
    expect(
      chapterRows([
        { start_seconds: 0, title: "Intro", episode_id: null, source_timestamp: null, url: null },
        { start_seconds: 75, title: "Pricing", episode_id: "a", source_timestamp: 754, url: "u" },
      ]).map((r) => [r.clock, r.title, r.href]),
    ).toEqual([
      ["0:00", "Intro", null],
      ["1:15", "Pricing", "u"],
    ]);
  });
});
