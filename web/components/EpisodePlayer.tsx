"use client";

import { useEffect, useState } from "react";

import { fetchAudioObjectUrl } from "@/lib/api-client";
import type { Script } from "@/lib/api-types";
import { MOCK_MODE } from "@/lib/config";
import { secondsToClock, youtubeDeepLink } from "@/lib/timeline";

const TAKE_TYPE_LABEL: Record<string, string> = {
  idea: "Idea",
  pushback: "Pushback",
  connection: "Connection",
  question: "Question",
  cross_reference: "Cross-reference",
};

function AudioSection({
  baseUrl,
  token,
  audioUrl,
}: {
  baseUrl: string;
  token: string;
  audioUrl: string | null;
}) {
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!audioUrl) return;
    let cancelled = false;
    let created: string | null = null;
    // Clear the previous episode's result before fetching the new one —
    // the fetch's own .then/.catch below update state from the async
    // result, which is the pattern this lint rule expects.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setError(null);
    setObjectUrl(null);
    fetchAudioObjectUrl(baseUrl, token, audioUrl)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        created = url;
        setObjectUrl(url);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Could not load audio.");
      });
    return () => {
      cancelled = true;
      if (created) URL.revokeObjectURL(created);
    };
  }, [baseUrl, token, audioUrl]);

  if (!audioUrl) {
    return <p className="font-serif text-sm italic text-silver">Audio not available for this job.</p>;
  }
  if (error) {
    return <p className="font-serif text-sm italic text-silver">{error}</p>;
  }
  if (!objectUrl) {
    return <p className="label-caps">Loading audio…</p>;
  }
  return <audio controls src={objectUrl} className="w-full" />;
}

export function EpisodePlayer({
  baseUrl,
  token,
  audioUrl,
  script,
}: {
  baseUrl: string;
  token: string;
  audioUrl: string | null;
  script: Script | null;
}) {
  return (
    <section aria-label="Episode player" className="space-y-4">
      <AudioSection baseUrl={baseUrl} token={token} audioUrl={audioUrl} />
      {MOCK_MODE && audioUrl ? (
        <p className="label-caps text-blue">Mock mode — audio artifacts are not served.</p>
      ) : null}

      {script ? (
        <>
          <div>
            <p className="label-caps mb-2">Beats</p>
            <ol className="space-y-2">
              {script.takes.map((take, i) => {
                const href = youtubeDeepLink(take.episode_id, take.segment_timestamp);
                return (
                  <li key={i} className="border-t border-taupe pt-2 first:border-t-0">
                    <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                      <span className="label-caps text-navy">
                        {TAKE_TYPE_LABEL[take.take_type] ?? take.take_type}
                      </span>
                      <span className="font-mono text-xs text-silver">
                        {take.episode_id} @ {secondsToClock(take.segment_timestamp)}
                      </span>
                      {href ? (
                        <a href={href} target="_blank" rel="noopener noreferrer" className="text-xs text-blue underline">
                          watch
                        </a>
                      ) : null}
                    </div>
                    <p className="mt-1 font-serif text-sm text-ink">{take.text}</p>
                  </li>
                );
              })}
            </ol>
          </div>

          <div>
            <p className="label-caps mb-2">Monologue</p>
            <p className="whitespace-pre-wrap font-serif text-[15px] leading-relaxed text-ink">
              {script.monologue}
            </p>
          </div>
        </>
      ) : (
        <p className="font-serif text-sm italic text-silver">Script not available for this job.</p>
      )}
    </section>
  );
}
