"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";

import {
  ApiError,
  fetchSampleSoul,
  listShows,
  submitDigest,
  submitDigestSelect,
} from "@/lib/api-client";
import { MAX_HIGHLIGHTS, MIN_HIGHLIGHTS } from "@/lib/api-types";
import type { ShowListing } from "@/lib/api-types";
import { MOCK_MODE } from "@/lib/config";
import { parseVideoIds } from "@/lib/parse";
import {
  addRecentJob,
  clearRecentJobs,
  getBaseUrl,
  getRecentJobs,
  getToken,
  setBaseUrl as persistBaseUrl,
  setToken as persistToken,
  type RecentJob,
} from "@/lib/storage";

type SelectionMode = "catalog" | "explicit";
type SoulOrigin = "supplied" | "derived" | "interview" | "seed";

export function SubmitForm() {
  const router = useRouter();

  const [baseUrl, setBaseUrlState] = useState("");
  const [token, setTokenState] = useState("");
  const [hydrated, setHydrated] = useState(false);

  const [shows, setShows] = useState<ShowListing[]>([]);
  const [showsError, setShowsError] = useState<string | null>(null);
  const [showsLoading, setShowsLoading] = useState(false);

  const [soul, setSoul] = useState("");
  const [context, setContext] = useState("");
  const [soulOrigin, setSoulOrigin] = useState<SoulOrigin>("supplied");
  const [highlightCount, setHighlightCount] = useState(4);

  const [mode, setMode] = useState<SelectionMode>("catalog");
  const [selectedVideoIds, setSelectedVideoIds] = useState<Set<string>>(new Set());
  const [explicitIdsText, setExplicitIdsText] = useState("");

  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const [recentJobs, setRecentJobs] = useState<RecentJob[]>([]);

  useEffect(() => {
    // One-time hydration from localStorage — unavailable during SSR.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setBaseUrlState(getBaseUrl());
    setTokenState(getToken());
    setRecentJobs(getRecentJobs());
    setHydrated(true);
  }, []);

  async function loadShows(url: string, tok: string) {
    setShowsLoading(true);
    setShowsError(null);
    try {
      const result = await listShows(url, tok);
      setShows(result);
    } catch (err) {
      setShowsError(err instanceof ApiError ? err.message : "Could not load shows.");
    } finally {
      setShowsLoading(false);
    }
  }

  useEffect(() => {
    if (!hydrated) return;
    // loadShows sets state from its own try/catch/finally, which is the
    // pattern this lint rule wants — only its synchronous first lines
    // (loading=true) run in this effect's call stack.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void loadShows(baseUrl, token);
    // Only re-run when the user explicitly reconnects (button below) or on
    // first hydration — not on every keystroke in the base URL/token fields.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hydrated]);

  function toggleVideoId(id: string) {
    setSelectedVideoIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function loadSample(name: "investor" | "popculture") {
    try {
      const text = await fetchSampleSoul(name);
      setSoul(text);
    } catch {
      setSubmitError("Could not load the sample soul.");
    }
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    setSubmitError(null);

    if (soul.trim().length === 0) {
      setSubmitError("Soul is required.");
      return;
    }

    const requestedIds =
      mode === "catalog" ? [...selectedVideoIds] : parseVideoIds(explicitIdsText);
    if (requestedIds.length === 0) {
      setSubmitError(
        mode === "catalog"
          ? "Select at least one episode."
          : "Enter at least one video id.",
      );
      return;
    }

    setSubmitting(true);
    try {
      const { job_id } =
        mode === "catalog"
          ? await submitDigestSelect(baseUrl, token, {
              soul,
              context,
              video_ids: requestedIds,
              highlight_count: highlightCount,
              soul_origin: soulOrigin,
            })
          : await submitDigest(baseUrl, token, {
              soul,
              context,
              episodes: requestedIds.map((video_id) => ({ video_id })),
              highlight_count: highlightCount,
              soul_origin: soulOrigin,
            });

      addRecentJob({
        job_id,
        base_url: baseUrl,
        created_at: new Date().toISOString(),
        label: `${requestedIds.length} episode${requestedIds.length === 1 ? "" : "s"} · ${soulOrigin}`,
        requested_episode_ids: requestedIds,
      });
      router.push(`/jobs/${job_id}`);
    } catch (err) {
      setSubmitError(err instanceof ApiError ? err.message : "Could not submit the digest.");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="space-y-10">
      <header className="space-y-2">
        <h1 className="font-serif text-2xl text-navy sm:text-[28px]">Chorus Viewer</h1>
        <p className="max-w-2xl font-serif text-[15px] leading-relaxed text-ink">
          Submit a soul and a set of episodes; Chorus reads every transcript, surfaces what
          clears your lens, and renders a short audio episode in your voice.
        </p>
        {MOCK_MODE ? (
          <p className="label-caps text-blue">
            Mock mode — requests are served from web/mocks/, no API required.
          </p>
        ) : null}
      </header>

      <section className="space-y-3">
        <h2 className="label-caps">Connect</h2>
        <div className="grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="label-caps mb-1 block">Base URL</span>
            <input
              type="text"
              value={baseUrl}
              onChange={(e) => {
                setBaseUrlState(e.target.value);
                persistBaseUrl(e.target.value);
              }}
              placeholder="https://chorus.up.railway.app"
              className="w-full border border-taupe bg-surface px-3 py-2 font-mono text-sm text-ink"
            />
          </label>
          <label className="block">
            <span className="label-caps mb-1 block">Bearer token</span>
            <input
              type="password"
              value={token}
              onChange={(e) => {
                setTokenState(e.target.value);
                persistToken(e.target.value);
              }}
              placeholder="CHORUS_API_TOKEN"
              className="w-full border border-taupe bg-surface px-3 py-2 font-mono text-sm text-ink"
            />
          </label>
        </div>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={() => void loadShows(baseUrl, token)}
            className="border border-navy px-3 py-1.5 font-sans text-xs uppercase tracking-wide text-navy hover:bg-navy hover:text-ivory"
          >
            {showsLoading ? "Connecting…" : "Reconnect"}
          </button>
          {showsError ? <span className="font-mono text-xs text-red">{showsError}</span> : null}
        </div>
      </section>

      <form onSubmit={handleSubmit} className="space-y-8">
        <section className="space-y-2">
          <div className="flex items-baseline justify-between">
            <h2 className="label-caps">Soul</h2>
            <div className="flex gap-3 font-sans text-xs">
              <button
                type="button"
                onClick={() => void loadSample("investor")}
                className="text-blue underline"
              >
                Load sample: investor
              </button>
              <button
                type="button"
                onClick={() => void loadSample("popculture")}
                className="text-blue underline"
              >
                Load sample: pop-culture
              </button>
            </div>
          </div>
          <textarea
            value={soul}
            onChange={(e) => setSoul(e.target.value)}
            rows={10}
            placeholder="# Soul — your lens as markdown"
            className="w-full border border-taupe bg-surface px-3 py-2 font-mono text-xs text-ink"
          />
        </section>

        <section className="space-y-2">
          <h2 className="label-caps">Context</h2>
          <textarea
            value={context}
            onChange={(e) => setContext(e.target.value)}
            rows={5}
            placeholder="What the principal is working on/reading this week."
            className="w-full border border-taupe bg-surface px-3 py-2 font-mono text-xs text-ink"
          />
        </section>

        <section className="space-y-3">
          <h2 className="label-caps">Episodes</h2>
          <div className="flex gap-4 font-sans text-sm">
            <label className="flex items-center gap-1.5">
              <input
                type="radio"
                checked={mode === "catalog"}
                onChange={() => setMode("catalog")}
              />
              From catalog (shows)
            </label>
            <label className="flex items-center gap-1.5">
              <input
                type="radio"
                checked={mode === "explicit"}
                onChange={() => setMode("explicit")}
              />
              Explicit video ids
            </label>
          </div>

          {mode === "catalog" ? (
            <div className="max-h-72 space-y-4 overflow-y-auto border border-taupe p-3">
              {shows.length === 0 ? (
                <p className="font-serif text-sm italic text-silver">
                  {showsLoading ? "Loading shows…" : "No shows loaded yet."}
                </p>
              ) : (
                shows.map((show) => (
                  <div key={show.show}>
                    <p className="mb-1 font-sans text-xs font-semibold uppercase tracking-wide text-navy">
                      {show.show}
                    </p>
                    <ul className="space-y-1">
                      {show.episodes.map((ep) => (
                        <li key={ep.video_id}>
                          <label className="flex items-start gap-1.5 font-serif text-sm">
                            <input
                              type="checkbox"
                              checked={selectedVideoIds.has(ep.video_id)}
                              onChange={() => toggleVideoId(ep.video_id)}
                              className="mt-1"
                            />
                            <span>
                              {ep.title ?? ep.video_id}{" "}
                              <span className="font-mono text-xs text-silver">
                                {ep.video_id}
                              </span>
                            </span>
                          </label>
                        </li>
                      ))}
                    </ul>
                  </div>
                ))
              )}
            </div>
          ) : (
            <textarea
              value={explicitIdsText}
              onChange={(e) => setExplicitIdsText(e.target.value)}
              rows={4}
              placeholder={"One YouTube video id per line, e.g.\ngs39QFYIbBY"}
              className="w-full border border-taupe bg-surface px-3 py-2 font-mono text-xs text-ink"
            />
          )}
        </section>

        <section className="grid gap-3 sm:grid-cols-2">
          <label className="block">
            <span className="label-caps mb-1 block">Highlight count</span>
            <input
              type="number"
              min={MIN_HIGHLIGHTS}
              max={MAX_HIGHLIGHTS}
              value={highlightCount}
              onChange={(e) =>
                setHighlightCount(
                  Math.min(MAX_HIGHLIGHTS, Math.max(MIN_HIGHLIGHTS, Number(e.target.value) || 1)),
                )
              }
              className="w-full border border-taupe bg-surface px-3 py-2 font-mono text-sm text-ink"
            />
          </label>
          <label className="block">
            <span className="label-caps mb-1 block">Soul origin</span>
            <select
              value={soulOrigin}
              onChange={(e) => setSoulOrigin(e.target.value as SoulOrigin)}
              className="w-full border border-taupe bg-surface px-3 py-2 font-sans text-sm text-ink"
            >
              <option value="supplied">supplied</option>
              <option value="derived">derived</option>
              <option value="interview">interview</option>
              <option value="seed">seed</option>
            </select>
          </label>
        </section>

        {submitError ? <p className="font-mono text-sm text-red">{submitError}</p> : null}

        <button
          type="submit"
          disabled={submitting}
          className="bg-navy px-5 py-2.5 font-sans text-sm uppercase tracking-wide text-ivory disabled:opacity-50"
        >
          {submitting ? "Submitting…" : "Submit digest"}
        </button>
      </form>

      <section className="space-y-2">
        <div className="flex items-baseline justify-between">
          <h2 className="label-caps">Recent jobs</h2>
          {recentJobs.length > 0 ? (
            <button
              type="button"
              onClick={() => {
                clearRecentJobs();
                setRecentJobs([]);
              }}
              className="font-sans text-xs text-silver underline"
            >
              Clear
            </button>
          ) : null}
        </div>
        {recentJobs.length === 0 ? (
          <p className="font-serif text-sm italic text-silver">No jobs submitted yet.</p>
        ) : (
          <ul className="space-y-1">
            {recentJobs.map((j) => (
              <li key={j.job_id} className="flex items-baseline gap-3 font-serif text-sm">
                <a href={`/jobs/${j.job_id}`} className="text-blue underline">
                  <span className="font-mono">{j.job_id}</span>
                </a>
                <span className="text-silver">{j.label}</span>
                <span className="label-caps">{new Date(j.created_at).toLocaleString()}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
