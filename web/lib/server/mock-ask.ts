/**
 * Mock-mode stand-in for POST /digest/{job_id}/ask (chorus/ask.py). The real
 * endpoint answers from transcripts; mock mode has only the job's highlights,
 * so it quotes the highlights that share content words with the question and
 * refuses when none do, the same contract the real endpoint keeps.
 */
import type { AskAnswer, Job } from "@/lib/api-types";
import { allHighlights } from "@/lib/api-types";

const MIN_WORD_CHARS = 4;
const MAX_SENTENCES = 2;
export const MOCK_REFUSAL = "nothing in this digest's episodes speaks to that";

function words(text: string): Set<string> {
  return new Set(
    (text.toLowerCase().match(/[a-z][a-z'-]+/g) ?? []).filter((w) => w.length >= MIN_WORD_CHARS),
  );
}

export function mockAnswer(job: Job, question: string): AskAnswer {
  const asked = words(question);
  const scored = (job.digest ? allHighlights(job.digest) : [])
    .map((h) => ({ h, shared: [...words(h.quote)].filter((w) => asked.has(w)).length }))
    .filter((x) => x.shared > 0)
    .sort((a, b) => b.shared - a.shared)
    .slice(0, MAX_SENTENCES);
  if (scored.length === 0) {
    return { question, sentences: [], refused: true, refusal_reason: MOCK_REFUSAL, unavailable: [] };
  }
  return {
    question,
    sentences: scored.map(({ h }) => ({
      text: `${h.episode_title ?? h.episode_id} speaks to this directly.`,
      citations: [{ episode_id: h.episode_id, segment_timestamp: h.segment_timestamp, quote: h.quote }],
    })),
    refused: false,
    refusal_reason: null,
    unavailable: [],
  };
}
