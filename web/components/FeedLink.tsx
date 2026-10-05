"use client";

import { useEffect, useState } from "react";

import { fetchFeed } from "@/lib/api-client";
import type { FeedInfo } from "@/lib/api-types";

/** The principal's private podcast feed (chorus/podcast_feed.py): one URL
 * to add to a podcast app, after which every digest arrives on its own. */
export function FeedLink({ baseUrl, token }: { baseUrl: string; token: string }) {
  const [feed, setFeed] = useState<FeedInfo | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchFeed(baseUrl, token)
      .then((info) => {
        if (!cancelled) setFeed(info);
      })
      .catch((err: unknown) => {
        if (!cancelled) setError(err instanceof Error ? err.message : "Could not load your feed.");
      });
    return () => {
      cancelled = true;
    };
  }, [baseUrl, token]);

  async function copy() {
    if (!feed) return;
    try {
      await navigator.clipboard.writeText(feed.feed_url);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }

  if (error) return <p className="font-serif text-sm italic text-silver">{error}</p>;
  if (!feed) return null;

  return (
    <section className="space-y-2 border border-taupe bg-surface px-4 py-3" aria-label="Podcast feed">
      <p className="label-caps">Your podcast feed · {feed.episodes} episode(s)</p>
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
        <code className="flex-1 break-all font-mono text-xs text-ink">{feed.feed_url}</code>
        <button
          type="button"
          onClick={copy}
          className="border border-navy px-3 py-1.5 font-sans text-xs uppercase tracking-wide text-navy"
        >
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <p className="font-serif text-xs italic text-silver">{feed.instructions}</p>
    </section>
  );
}
