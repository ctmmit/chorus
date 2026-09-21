import { describe, expect, it } from "vitest";
import {
  highlightTimestamps,
  isHighlightWindow,
  isYouTubeVideoId,
  overlapPercentage,
  resolveDurationSeconds,
  secondsToClock,
  tickHeightForScore,
  xForTime,
  youtubeDeepLink,
} from "./timeline";

describe("isYouTubeVideoId", () => {
  it("accepts 11-char YouTube ids", () => {
    expect(isYouTubeVideoId("c4tvVKDhpiY")).toBe(true);
    expect(isYouTubeVideoId("gs39QFYIbBY")).toBe(true);
  });

  it("rejects ids of the wrong shape", () => {
    expect(isYouTubeVideoId("too-short")).toBe(false);
    expect(isYouTubeVideoId("way-too-long-to-be-a-video-id")).toBe(false);
    expect(isYouTubeVideoId("")).toBe(false);
  });
});

describe("youtubeDeepLink", () => {
  it("builds a watch URL with an integer second offset", () => {
    expect(youtubeDeepLink("c4tvVKDhpiY", 1800.4)).toBe(
      "https://www.youtube.com/watch?v=c4tvVKDhpiY&t=1800s",
    );
  });

  it("returns null for a non-YouTube-shaped id", () => {
    expect(youtubeDeepLink("not-a-video-id", 10)).toBeNull();
  });

  it("clamps negative timestamps to 0", () => {
    expect(youtubeDeepLink("c4tvVKDhpiY", -5)).toBe(
      "https://www.youtube.com/watch?v=c4tvVKDhpiY&t=0s",
    );
  });
});

describe("secondsToClock", () => {
  it("formats under an hour as mm:ss", () => {
    expect(secondsToClock(0)).toBe("0:00");
    expect(secondsToClock(65)).toBe("1:05");
    expect(secondsToClock(3599)).toBe("59:59");
  });

  it("formats an hour or more as h:mm:ss", () => {
    expect(secondsToClock(3600)).toBe("1:00:00");
    expect(secondsToClock(3725)).toBe("1:02:05");
  });

  it("clamps invalid input to 0:00", () => {
    expect(secondsToClock(-10)).toBe("0:00");
    expect(secondsToClock(NaN)).toBe("0:00");
  });
});

describe("resolveDurationSeconds", () => {
  it("prefers the server-reported duration", () => {
    expect(
      resolveDurationSeconds({
        duration_seconds: 500,
        windows: [{ start: 1000, score: 0.9 }],
        highlights: [],
      }),
    ).toBe(500);
  });

  it("falls back to the latest window/highlight start plus padding", () => {
    expect(
      resolveDurationSeconds({
        duration_seconds: null,
        windows: [{ start: 100, score: 0.1 }],
        highlights: [
          {
            episode_id: "e",
            episode_title: null,
            segment_timestamp: 400,
            quote: "q",
            relevance_score: 0.5,
            why_surface: "w",
          },
        ],
      }),
    ).toBe(430);
  });

  it("returns 0 when there is nothing to infer from", () => {
    expect(resolveDurationSeconds({ duration_seconds: null, windows: [], highlights: [] })).toBe(
      0,
    );
  });
});

describe("xForTime", () => {
  it("scales linearly across the bar width", () => {
    expect(xForTime(0, 100, 900)).toBe(0);
    expect(xForTime(50, 100, 900)).toBe(450);
    expect(xForTime(100, 100, 900)).toBe(900);
  });

  it("clamps out-of-range timestamps", () => {
    expect(xForTime(-10, 100, 900)).toBe(0);
    expect(xForTime(1000, 100, 900)).toBe(900);
  });

  it("returns 0 for a non-positive duration instead of dividing by zero", () => {
    expect(xForTime(10, 0, 900)).toBe(0);
  });
});

describe("tickHeightForScore", () => {
  it("scales score 0..1 into minHeight..maxHeight", () => {
    expect(tickHeightForScore(0, 40, 2)).toBe(2);
    expect(tickHeightForScore(1, 40, 2)).toBe(40);
    expect(tickHeightForScore(0.5, 42, 2)).toBe(22);
  });

  it("clamps out-of-range scores", () => {
    expect(tickHeightForScore(-1, 40, 2)).toBe(2);
    expect(tickHeightForScore(2, 40, 2)).toBe(40);
  });
});

describe("isHighlightWindow / highlightTimestamps", () => {
  it("matches a window to a highlight within tolerance", () => {
    const ts = highlightTimestamps({
      episode_id: "e",
      episode_title: null,
      refused: false,
      refusal_reason: null,
      duration_seconds: null,
      windows: [],
      highlights: [
        {
          episode_id: "e",
          episode_title: null,
          segment_timestamp: 1800.4,
          quote: "q",
          relevance_score: 0.9,
          why_surface: "w",
        },
      ],
    });
    expect(isHighlightWindow(1800.0, ts)).toBe(true);
    expect(isHighlightWindow(1800.9, ts)).toBe(true);
    expect(isHighlightWindow(1900.0, ts)).toBe(false);
  });
});

describe("overlapPercentage", () => {
  it("is 100 for identical timestamp sets", () => {
    expect(overlapPercentage([10, 20, 30], [10, 20, 30])).toBe(100);
  });

  it("is 0 for disjoint sets", () => {
    expect(overlapPercentage([10, 20], [500, 600])).toBe(0);
  });

  it("computes Jaccard overlap for partial agreement", () => {
    // union {10,20,30}, shared {10,20} -> 2/3
    expect(overlapPercentage([10, 20], [10, 20, 30])).toBeCloseTo((2 / 3) * 100, 5);
  });

  it("treats two empty sets as 0% overlap", () => {
    expect(overlapPercentage([], [])).toBe(0);
  });
});
