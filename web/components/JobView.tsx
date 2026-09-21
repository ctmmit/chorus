"use client";

import { useEffect, useMemo, useState } from "react";

import { EpisodePlayer } from "@/components/EpisodePlayer";
import { EpisodeTimeline } from "@/components/EpisodeTimeline";
import { HighlightCard } from "@/components/HighlightCard";
import { ProvenanceLine } from "@/components/ProvenanceLine";
import { StatusBadge } from "@/components/StatusBadge";
import { allHighlights } from "@/lib/api-types";
import { buildClaimTitle } from "@/lib/claim";
import { getBaseUrl, getRecentJob, getToken } from "@/lib/storage";
import { skippedEpisodeIds } from "@/lib/timeline";
import { useJob } from "@/lib/useJob";

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

  const recentJob = useMemo(
    () => (hydrated ? getRecentJob(jobId) : undefined),
    [jobId, hydrated],
  );
  const { job, error, loading } = useJob(baseUrl, token, hydrated ? jobId : "");

  if (!hydrated || (loading && !job)) {
    return <p className="label-caps">Loading…</p>;
  }

  if (error && !job) {
    return (
      <div className="space-y-2">
        <p className="font-serif text-lg text-ink">Could not reach the Chorus API.</p>
        <p className="font-mono text-sm text-red">{error}</p>
      </div>
    );
  }

  if (!job) {
    return <p className="label-caps">Loading…</p>;
  }

  const skipped = recentJob
    ? skippedEpisodeIds(recentJob.requested_episode_ids, job.digest?.episodes ?? [])
    : [];

  return (
    <div className="space-y-10">
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
              {buildClaimTitle(job.digest, recentJob?.requested_episode_ids.length)}
            </h1>
            <ProvenanceLine
              soulVersion={job.digest.soul_version}
              soulOrigin={job.digest.soul_origin}
            />
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
          <section className="space-y-3">
            <h2 className="label-caps">Episode timeline</h2>
            <EpisodeTimeline episodes={job.digest.episodes} skippedEpisodeIds={skipped} />
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
                .map((h, i) => <HighlightCard key={`${h.episode_id}-${i}`} highlight={h} />)
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
              />
            </section>
          ) : (
            <p className="label-caps">Script and audio render after the digest completes.</p>
          )}
        </>
      ) : null}
    </div>
  );
}
