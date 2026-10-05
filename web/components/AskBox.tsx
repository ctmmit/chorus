"use client";

import { useState } from "react";

import { askDigest } from "@/lib/api-client";
import type { AskAnswer } from "@/lib/api-types";
import { secondsToClock, youtubeDeepLink } from "@/lib/timeline";

const MAX_QUESTION_CHARS = 500;

/** Ask this digest a question; answered only from its own transcripts,
 * every sentence quoting a moment, or refused (chorus/ask.py). */
export function AskBox({ baseUrl, token, jobId }: { baseUrl: string; token: string; jobId: string }) {
  const [question, setQuestion] = useState("");
  const [answer, setAnswer] = useState<AskAnswer | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    const q = question.trim();
    if (!q) return;
    setBusy(true);
    setError(null);
    try {
      setAnswer(await askDigest(baseUrl, token, jobId, q));
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not ask that.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="space-y-3" aria-label="Ask this digest">
      <h2 className="label-caps">Ask this digest</h2>
      <form onSubmit={submit} className="flex flex-col gap-2 sm:flex-row">
        <input
          value={question}
          maxLength={MAX_QUESTION_CHARS}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="What did they say about pricing?"
          className="flex-1 border border-taupe bg-surface px-3 py-2 font-serif text-sm text-ink"
        />
        <button
          type="submit"
          disabled={busy || !question.trim()}
          className="border border-navy px-3 py-2 font-sans text-xs uppercase tracking-wide text-navy disabled:opacity-50"
        >
          {busy ? "Asking…" : "Ask"}
        </button>
      </form>
      {error ? <p className="font-mono text-sm text-red">{error}</p> : null}
      {answer ? (
        answer.refused ? (
          <p className="font-serif text-sm italic text-silver">
            {answer.refusal_reason ?? "Nothing in these episodes answers that."}
          </p>
        ) : (
          <ol className="space-y-3">
            {answer.sentences.map((s, i) => (
              <li key={i} className="font-serif text-[15px] leading-relaxed text-ink">
                {s.text}
                {s.citations.map((c, j) => {
                  const href = youtubeDeepLink(c.episode_id, c.segment_timestamp);
                  const clock = secondsToClock(c.segment_timestamp);
                  return (
                    <span key={j} className="mt-1 block font-sans text-xs text-silver">
                      “{c.quote}” ·{" "}
                      {href ? (
                        <a href={href} target="_blank" rel="noopener noreferrer" className="text-blue underline">
                          <span className="font-mono">{clock}</span>
                        </a>
                      ) : (
                        <span className="font-mono">{clock}</span>
                      )}
                    </span>
                  );
                })}
              </li>
            ))}
          </ol>
        )
      ) : null}
      {answer && answer.unavailable.length > 0 ? (
        <p className="font-serif text-xs italic text-silver">
          Could not re-read: {answer.unavailable.join(", ")}
        </p>
      ) : null}
    </section>
  );
}
