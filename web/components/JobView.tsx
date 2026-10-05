"use client";

import { useEffect, useState } from "react";

import { AskBox } from "@/components/AskBox";
import { EpisodePlayer } from "@/components/EpisodePlayer";
import { EpisodeTimeline } from "@/components/EpisodeTimeline";
import { HighlightCard } from "@/components/HighlightCard";
import { ProvenanceLine } from "@/components/ProvenanceLine";
import { StatusBadge } from "@/components/StatusBadge";
import { ThreadsSection } from "@/components/ThreadsSection";
import { UsageSummary } from "@/components/UsageSummary";
import { allHighlights } from "@/lib/api-types";
import { buildClaimTitle } from "@/lib/claim";
import { MAX_ELAPSED_MS } from "@/lib/polling";
import { getBaseUrl, getToken } from "@/lib/storage";
import { useJob } from "@/lib/useJob";

const MAX_ELAPSED_MINUTES = Math.round(MAX_ELAPSED_MS / 60_000);

function RetryButton({ onRetry }: { onRetry: () => void }) {
  return (
    <button
      type="button"
      onClick={onRetry}
      className="border border-navy px-3 py-1.5 font-sans text-xs uppercase tracking-wide text-navy"
    >
      Retry
    </button>
  );
}

export function JobView({ jobId }: { jobId: string }) {
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    // One-time hydration from localStorage: it isn't available during SSR,
    // so there's no way to seed this state before the first client render.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setBaseUrl(getBaseUrl());
    setToken(getToken());
    setHydrated(true);
  }, []);

  const { job, error, loading, paused, retry } = useJob(baseUrl, token, hydrated ? jobId : "");

  if (!hydrated || (loading && !job)) {
    return <p className="label-caps">Loading…</p>;
  }

  if (error && !job) {
    return (
      <div className="space-y-2">
        <p className="font-serif text-lg text-ink">Could not reach the Chorus API.</p>
        <p className="font-mono text-sm text-red">{error}</p>
        {paused ? (
          <div className="flex items-center gap-3">
            <p className="label-caps text-silver">
              Paused after {MAX_ELAPSED_MINUTES} min of retries.
            </p>
            <RetryButton onRetry={retry} />
          </div>
        ) : null}
      </div>
    );
  }

  if (!job) {
    return <p className="label-caps">Loading…</p>;
  }

  const usage = job.usage;

  return (
    <div className="space-y-10">
      {paused ? (
        <div className="flex items-center justify-between gap-3 border border-taupe bg-surface px-4 py-3">
          <p className="font-mono text-sm text-silver">
            Paused after {MAX_ELAPSED_MINUTES} min without a result.
          </p>
          <RetryButton onRetry={retry} />
        </div>
      ) : null}

      <header className="space-y-2">
        <div className="flex items-baseline justify-between gap-3">
          <p className="label-caps">
            Job <span className="font-mono normal-case text-ink">{job.job_id}</span>
          </p>
          <StatusBadge status={job.status} />
        </div>

        {job.digest ? (
          <>
            <h1 className="font-serif text-2xl text-navy sm:text-[28px]">
              {buildClaimTitle(job.digest, usage?.skipped.length ?? 0)}
            </h1>
            <ProvenanceLine
              soulVersion={job.digest.soul_version}
              soulOrigin={job.digest.soul_origin}
            />
            {usage ? <UsageSummary usage={usage} /> : null}
          </>
        ) : (
          <h1 className="font-serif text-2xl text-navy sm:text-[28px]">
            {job.status === "queued" ? "Queued — waiting to start" : "Working…"}
          </h1>
        )}

        {job.error ? <p className="font-mono text-sm text-red">{job.error}</p> : null}
        {job.warnings.length > 0 ? (
          <ul className="space-y-1">
            {job.warnings.map((w, i) => (
              <li key={i} className="font-serif text-sm italic text-silver">
                {w}
              </li>
            ))}
          </ul>
        ) : null}
      </header>

      {job.digest ? (
        <>
          <ThreadsSection digest={job.digest} />

          <section className="space-y-3">
            <h2 className="label-caps">Episode timeline</h2>
            <EpisodeTimeline
              episodes={job.digest.episodes}
              transcriptSources={usage?.transcript_sources}
              skipped={usage?.skipped}
            />
          </section>

          <section className="space-y-1">
            <h2 className="label-caps">Highlights</h2>
            {allHighlights(job.digest).length === 0 ? (
              <p className="font-serif text-sm italic text-silver">
                Nothing cleared the relevance bar.
              </p>
            ) : (
              allHighlights(job.digest)
                .sort((a, b) => b.relevance_score - a.relevance_score)
                .map((h, i) => (
                  <HighlightCard
                    key={`${h.episode_id}-${i}`}
                    highlight={h}
                    rating={job.status === "done" ? { baseUrl, token, jobId: job.job_id } : undefined}
                  />
                ))
            )}
          </section>

          {job.status === "done" ? (
            <section className="space-y-3">
              <h2 className="label-caps">Episode player</h2>
              <EpisodePlayer
                baseUrl={baseUrl}
                token={token}
                audioUrl={job.audio_url}
                script={job.script}
                chapters={job.chapters}
              />
            </section>
          ) : (
            <p className="label-caps">Script and audio render after the digest completes.</p>
          )}

          {job.status === "done" ? (
            <AskBox baseUrl={baseUrl} token={token} jobId={job.job_id} />
          ) : null}
        </>
      ) : null}
    </div>
  );
}
