import { describe, expect, it } from "vitest";

import type { Job } from "@/lib/api-types";
import { MOCK_REFUSAL, mockAnswer } from "./mock-ask";

const job: Job = {
  job_id: "j",
  status: "done",
  script: null,
  audio_url: null,
  error: null,
  warnings: [],
  usage: null,
  digest: {
    soul_version: "v",
    soul_origin: "supplied",
    episodes: [
      {
        episode_id: "aaaaaaaaaaa",
        episode_title: "Margins",
        refused: false,
        refusal_reason: null,
        duration_seconds: null,
        windows: [],
        highlights: [
          { episode_id: "aaaaaaaaaaa", episode_title: "Margins", segment_timestamp: 12,
            quote: "Gross margins expand as inference costs fall.", relevance_score: 0.9, why_surface: "w" },
        ],
      },
    ],
  },
};

describe("mockAnswer", () => {
  it("quotes highlights that share words with the question", () => {
    const answer = mockAnswer(job, "what about gross margins?");
    expect(answer.refused).toBe(false);
    expect(answer.sentences[0].citations[0]).toEqual({
      episode_id: "aaaaaaaaaaa", segment_timestamp: 12,
      quote: "Gross margins expand as inference costs fall.",
    });
  });

  it("refuses when nothing speaks to it", () => {
    const answer = mockAnswer(job, "zebra xylophone");
    expect(answer).toMatchObject({ refused: true, refusal_reason: MOCK_REFUSAL, sentences: [] });
  });
});
