import { describe, expect, it } from "vitest";

import { buildClaimTitle } from "./claim";
import type { Digest } from "./api-types";

function digestWith(refusedFlags: boolean[], highlightsPerEpisode: number[]): Digest {
  return {
    soul_version: "abc",
    soul_origin: "supplied",
    episodes: refusedFlags.map((refused, i) => ({
      episode_id: `e${i}`,
      episode_title: null,
      refused,
      refusal_reason: refused ? "nothing cleared the relevance bar" : null,
      duration_seconds: 100,
      windows: [],
      highlights: refused
        ? []
        : Array.from({ length: highlightsPerEpisode[i] ?? 0 }, (_, j) => ({
            episode_id: `e${i}`,
            episode_title: null,
            segment_timestamp: j * 10,
            quote: "q",
            relevance_score: 0.5,
            why_surface: "w",
          })),
    })),
  };
}

describe("buildClaimTitle", () => {
  it("counts cleared episodes against the digest's own episode count by default", () => {
    const digest = digestWith([false, false, true], [4, 3, 0]);
    expect(buildClaimTitle(digest)).toBe("2 of 3 episodes cleared the bar; 7 highlights");
  });

  it("uses the requested total when the caller knows about skipped episodes", () => {
    const digest = digestWith([false, false, true], [4, 3, 0]);
    expect(buildClaimTitle(digest, 5)).toBe("2 of 5 episodes cleared the bar; 7 highlights");
  });

  it("singularizes episode/highlight when the count is exactly one", () => {
    const digest = digestWith([false], [1]);
    expect(buildClaimTitle(digest)).toBe("1 of 1 episode cleared the bar; 1 highlight");
  });
});
