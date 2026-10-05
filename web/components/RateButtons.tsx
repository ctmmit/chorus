"use client";

import { useState } from "react";

import { rateHighlight } from "@/lib/api-client";
import type { Vote } from "@/lib/api-types";

export interface RatingTarget {
  baseUrl: string;
  token: string;
  jobId: string;
}

/** "More like this" / "Less like this" on one highlight (chorus/feedback.py).
 * Ratings teach the lens only through a proposal the principal accepts, so
 * a click here never changes the soul by itself. */
export function RateButtons({ target, highlightId }: { target: RatingTarget; highlightId: string }) {
  const [vote, setVote] = useState<Vote | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function send(next: Vote) {
    setBusy(true);
    setError(null);
    try {
      await rateHighlight(target.baseUrl, target.token, {
        job_id: target.jobId,
        highlight_id: highlightId,
        vote: next,
      });
      setVote(next);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "Could not record that.");
    } finally {
      setBusy(false);
    }
  }

  const base = "border px-2 py-0.5 font-sans text-[11px] uppercase tracking-wide disabled:opacity-50";
  return (
    <span className="inline-flex items-center gap-2">
      <button
        type="button"
        disabled={busy}
        aria-pressed={vote === "up"}
        onClick={() => send("up")}
        className={`${base} ${vote === "up" ? "border-navy bg-navy text-paper" : "border-taupe text-navy"}`}
      >
        More like this
      </button>
      <button
        type="button"
        disabled={busy}
        aria-pressed={vote === "down"}
        onClick={() => send("down")}
        className={`${base} ${vote === "down" ? "border-navy bg-navy text-paper" : "border-taupe text-navy"}`}
      >
        Less like this
      </button>
      {error ? <span className="font-serif text-xs italic text-silver">{error}</span> : null}
    </span>
  );
}
