"use client";

import { useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";

import { EpisodeTimeline } from "@/components/EpisodeTimeline";
import { HighlightCard } from "@/components/HighlightCard";
import { ProvenanceLine } from "@/components/ProvenanceLine";
import { StatusBadge } from "@/components/StatusBadge";
import { allHighlights, type Digest, type EpisodeDigest, type Job } from "@/lib/api-types";
import { getBaseUrl, getToken } from "@/lib/storage";
import { highlightTimestamps, overlapPercentage } from "@/lib/timeline";
import { useJob } from "@/lib/useJob";

function CompareSide({ label, job, error }: { label: string; job: Job | null; error: string | null }) {
  if (error) return <p className="font-mono text-sm text-red">{error}</p>;
  if (!job) return <p className="label-caps">Loading {label}…</p>;

  return (
    <div className="space-y-4">
      <div className="flex items-baseline justify-between gap-3">
        <p className="label-caps">
          {label} <span className="font-mono normal-case text-ink">{job.job_id}</span>
        </p>
        <StatusBadge status={job.status} />
      </div>
      {job.error ? <p className="font-mono text-sm text-red">{job.error}</p> : null}
      {job.digest ? (
        <>
          <ProvenanceLine soulVersion={job.digest.soul_version} soulOrigin={job.digest.soul_origin} />
          <EpisodeTimeline episodes={job.digest.episodes} />
          <div>
            {allHighlights(job.digest)
              .sort((a, b) => b.relevance_score - a.relevance_score)
              .map((h, i) => (
                <HighlightCard key={`${h.episode_id}-${i}`} highlight={h} />
              ))}
          </div>
        </>
      ) : (
        <p className="font-serif text-sm italic text-silver">Digest not ready yet.</p>
      )}
    </div>
  );
}

function sharedEpisodeOverlap(a: Digest, b: Digest): { episode_id: string; title: string; overlap: number }[] {
  const byId = new Map<string, EpisodeDigest>(b.episodes.map((e) => [e.episode_id, e]));
  const rows: { episode_id: string; title: string; overlap: number }[] = [];
  for (const epA of a.episodes) {
    const epB = byId.get(epA.episode_id);
    if (!epB) continue;
    rows.push({
      episode_id: epA.episode_id,
      title: epA.episode_title ?? epB.episode_title ?? epA.episode_id,
      overlap: overlapPercentage(highlightTimestamps(epA), highlightTimestamps(epB)),
    });
  }
  return rows;
}

export function CompareView({ jobIdA, jobIdB }: { jobIdA: string | null; jobIdB: string | null }) {
  const router = useRouter();
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [inputA, setInputA] = useState(jobIdA ?? "");
  const [inputB, setInputB] = useState(jobIdB ?? "");

  useEffect(() => {
    // One-time hydration from localStorage — unavailable during SSR.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setBaseUrl(getBaseUrl());
    setToken(getToken());
  }, []);

  const { job: jobA, error: errorA } = useJob(baseUrl, token, jobIdA ?? "");
  const { job: jobB, error: errorB } = useJob(baseUrl, token, jobIdB ?? "");

  const overlapRows = useMemo(() => {
    if (!jobA?.digest || !jobB?.digest) return [];
    return sharedEpisodeOverlap(jobA.digest, jobB.digest);
  }, [jobA, jobB]);

  const overallOverlap = useMemo(() => {
    if (overlapRows.length === 0) return null;
    return overlapRows.reduce((sum, r) => sum + r.overlap, 0) / overlapRows.length;
  }, [overlapRows]);

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <h1 className="font-serif text-2xl text-navy sm:text-[28px]">Soul diff</h1>
        <p className="max-w-2xl font-serif text-[15px] leading-relaxed text-ink">
          Two digests of the same episodes, side by side — the two-soul divergence made visible.
        </p>
      </header>

      <form
        onSubmit={(e) => {
          e.preventDefault();
          router.push(`/compare?a=${encodeURIComponent(inputA)}&b=${encodeURIComponent(inputB)}`);
        }}
        className="flex flex-wrap items-end gap-3"
      >
        <label className="block">
          <span className="label-caps mb-1 block">Job A</span>
          <input
            value={inputA}
            onChange={(e) => setInputA(e.target.value)}
            className="w-56 border border-taupe bg-surface px-3 py-2 font-mono text-sm text-ink"
          />
        </label>
        <label className="block">
          <span className="label-caps mb-1 block">Job B</span>
          <input
            value={inputB}
            onChange={(e) => setInputB(e.target.value)}
            className="w-56 border border-taupe bg-surface px-3 py-2 font-mono text-sm text-ink"
          />
        </label>
        <button
          type="submit"
          className="bg-navy px-4 py-2 font-sans text-xs uppercase tracking-wide text-ivory"
        >
          Compare
        </button>
      </form>

      {jobIdA && jobIdB ? (
        <>
          {overallOverlap !== null ? (
            <section className="space-y-2 border-t border-taupe pt-4">
              <h2 className="label-caps">Overlap on shared episodes</h2>
              <p className="font-serif text-lg text-navy">
                <span className="font-mono">{overallOverlap.toFixed(0)}%</span> average agreement
                across {overlapRows.length} shared episode{overlapRows.length === 1 ? "" : "s"}
              </p>
              <table className="w-full text-left text-sm">
                <thead>
                  <tr className="label-caps border-b border-taupe">
                    <th className="py-1 font-normal">Episode</th>
                    <th className="py-1 font-normal">Overlap</th>
                  </tr>
                </thead>
                <tbody>
                  {overlapRows.map((r) => (
                    <tr key={r.episode_id} className="border-b border-taupe">
                      <td className="py-1.5 font-serif text-ink">{r.title}</td>
                      <td className="py-1.5 font-mono text-navy">{r.overlap.toFixed(0)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>
          ) : jobA?.digest && jobB?.digest ? (
            <p className="font-serif text-sm italic text-silver">No shared episodes to compare.</p>
          ) : null}

          <div className="grid gap-8 border-t border-taupe pt-6 md:grid-cols-2">
            <CompareSide label="Job A" job={jobA} error={errorA} />
            <CompareSide label="Job B" job={jobB} error={errorB} />
          </div>
        </>
      ) : (
        <p className="font-serif text-sm italic text-silver">
          Enter two job ids above, or open this page as /compare?a=&lt;job&gt;&amp;b=&lt;job&gt;.
        </p>
      )}
    </div>
  );
}
