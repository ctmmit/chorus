"use client";

import { useEffect, useId, useRef, useState, type ChangeEvent, type FormEvent } from "react";

import {
  fetchSampleOpml,
  importOpml,
  resolvePodcast,
  searchPodcasts,
} from "@/lib/api-client";
import type { OpmlSkipped, PodcastSearchResult, Source } from "@/lib/api-types";
import { describeApiError, isAbortError } from "@/lib/api-errors";
import { MOCK_MODE } from "@/lib/config";
import {
  MAX_OPML_BYTES,
  insecureFeedSources,
  SEARCH_DEBOUNCE_MS,
  SEARCH_MIN_CHARS,
  mergeSources,
  removeSource,
  skippedLineLabel,
  sourceKey,
  sourceKindLabel,
  sourceTitle,
} from "@/lib/subscribe";

import { ArtworkThumb } from "./ArtworkThumb";
import {
  ERROR_TEXT_CLASS,
  FOCUS_RING_CLASS,
  HELP_TEXT_CLASS,
  INPUT_CLASS,
  SECONDARY_BUTTON_CLASS,
  TEXT_BUTTON_CLASS,
} from "./ui";

const SEARCH_LIMIT = 10;

interface SearchState {
  query: string;
  results: PodcastSearchResult[];
  error: string | null;
}

interface Note {
  tone: "info" | "error";
  text: string;
}

interface OpmlSummary {
  added: number;
  duplicates: number;
  skipped: OpmlSkipped[];
}

function sourceFromResult(result: PodcastSearchResult): Source {
  return {
    kind: "rss",
    feed_url: result.feed_url,
    title: result.title,
    artwork_url: result.artwork_url,
  };
}

function plural(n: number, one: string, many: string): string {
  return `${n} ${n === 1 ? one : many}`;
}

/**
 * Choose podcasts: search by name, paste a link (RSS, Apple Podcasts, YouTube
 * channel), or import an OPML export from a podcast app. The chosen list is
 * controlled by the parent (`sources` / `onChange`) so the wizard and the
 * /subscriptions editor can both host it.
 */
export function SourcePicker({
  sources,
  onChange,
  baseUrl,
  token,
  onAuthError,
}: {
  sources: Source[];
  onChange: (next: Source[]) => void;
  baseUrl: string;
  token: string;
  /** Called when the API rejects the key (401/403). Keep it referentially stable. */
  onAuthError?: () => void;
}) {
  const uid = useId();

  // The newest list, for async handlers that resolve after the list changed.
  const latest = useRef(sources);
  useEffect(() => {
    latest.current = sources;
  }, [sources]);

  function commit(next: Source[]) {
    latest.current = next;
    onChange(next);
  }

  const chosenKeys = new Set(sources.map(sourceKey));

  // --- Search -----------------------------------------------------------
  const [query, setQuery] = useState("");
  const [search, setSearch] = useState<SearchState>({ query: "", results: [], error: null });
  const [addedNote, setAddedNote] = useState("");

  const trimmed = query.trim();
  const searchActive = trimmed.length >= SEARCH_MIN_CHARS;
  const searchSettled = searchActive && search.query === trimmed;

  useEffect(() => {
    if (!searchActive) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      try {
        const results = await searchPodcasts(baseUrl, token, trimmed, SEARCH_LIMIT, controller.signal);
        setSearch({ query: trimmed, results, error: null });
      } catch (err) {
        if (controller.signal.aborted || isAbortError(err)) return;
        const described = describeApiError(err, "Search failed. Try again.");
        if (described.auth) onAuthError?.();
        setSearch({ query: trimmed, results: [], error: described.message });
      }
    }, SEARCH_DEBOUNCE_MS);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [searchActive, trimmed, baseUrl, token, onAuthError]);

  function addResult(result: PodcastSearchResult) {
    const { merged, added } = mergeSources(latest.current, [sourceFromResult(result)]);
    commit(merged);
    setAddedNote(added > 0 ? `Added ${result.title}.` : `${result.title} is already in your list.`);
  }

  let searchStatus = "";
  if (searchActive) {
    if (!searchSettled) searchStatus = "Searching…";
    else if (search.error) searchStatus = search.error;
    else if (search.results.length === 0) searchStatus = `No shows match “${trimmed}”. Try another name, or paste a link below.`;
    else searchStatus = `${plural(search.results.length, "show", "shows")} found.`;
  }

  // --- Paste a link -----------------------------------------------------
  const [link, setLink] = useState("");
  const [linkBusy, setLinkBusy] = useState(false);
  const [linkNote, setLinkNote] = useState<Note | null>(null);

  async function handleLink(e: FormEvent) {
    e.preventDefault();
    const url = link.trim();
    if (!url) {
      setLinkNote({ tone: "error", text: "Paste a link first." });
      return;
    }
    setLinkBusy(true);
    setLinkNote(null);
    try {
      const source = await resolvePodcast(baseUrl, token, url);
      const { merged, added } = mergeSources(latest.current, [source]);
      commit(merged);
      setLink("");
      setLinkNote({
        tone: "info",
        text: added > 0 ? `Added ${sourceTitle(source)}.` : `${sourceTitle(source)} is already in your list.`,
      });
    } catch (err) {
      const described = describeApiError(err, "Could not read that link.");
      if (described.auth) onAuthError?.();
      setLinkNote({ tone: "error", text: described.message });
    } finally {
      setLinkBusy(false);
    }
  }

  // --- OPML import ------------------------------------------------------
  const [opmlBusy, setOpmlBusy] = useState(false);
  const [opmlNote, setOpmlNote] = useState<Note | null>(null);
  const [opmlSummary, setOpmlSummary] = useState<OpmlSummary | null>(null);

  async function importText(text: string) {
    setOpmlBusy(true);
    setOpmlNote(null);
    setOpmlSummary(null);
    try {
      const result = await importOpml(baseUrl, token, text);
      const { merged, added, duplicates } = mergeSources(latest.current, result.sources);
      commit(merged);
      setOpmlSummary({ added, duplicates, skipped: result.skipped });
    } catch (err) {
      const described = describeApiError(err, "Could not read that file.");
      if (described.auth) onAuthError?.();
      setOpmlNote({ tone: "error", text: described.message });
    } finally {
      setOpmlBusy(false);
    }
  }

  async function handleFile(e: ChangeEvent<HTMLInputElement>) {
    const input = e.target;
    const file = input.files?.[0];
    if (!file) return;
    if (file.size > MAX_OPML_BYTES) {
      setOpmlSummary(null);
      setOpmlNote({
        tone: "error",
        text: `That file is larger than ${MAX_OPML_BYTES / 1_000_000} MB. Export only your subscriptions, not your whole library.`,
      });
      input.value = "";
      return;
    }
    try {
      await importText(await file.text());
    } finally {
      // Let the same file be chosen again.
      input.value = "";
    }
  }

  async function handleSample() {
    try {
      await importText(await fetchSampleOpml());
    } catch (err) {
      setOpmlNote({ tone: "error", text: describeApiError(err, "Could not load the sample file.").message });
    }
  }

  const linkId = `${uid}-link`;
  const linkHelpId = `${uid}-link-help`;
  const searchId = `${uid}-search`;
  const searchStatusId = `${uid}-search-status`;
  const fileId = `${uid}-opml`;
  const fileHelpId = `${uid}-opml-help`;

  return (
    <div className="space-y-10">
      {/* Search */}
      <section aria-labelledby={`${uid}-search-h`} className="space-y-3">
        <h3 id={`${uid}-search-h`} className="label-caps">
          Search by name
        </h3>
        <div>
          <label htmlFor={searchId} className="sr-only">
            Search podcasts by name
          </label>
          <input
            id={searchId}
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Acquired, Odd Lots, Masters of Scale…"
            autoComplete="off"
            aria-describedby={searchStatusId}
            className={INPUT_CLASS}
          />
        </div>
        <p
          id={searchStatusId}
          role={searchSettled && search.error ? "alert" : "status"}
          aria-live="polite"
          className={searchSettled && search.error ? ERROR_TEXT_CLASS : HELP_TEXT_CLASS}
        >
          {searchStatus || (searchActive ? "" : `Type at least ${SEARCH_MIN_CHARS} letters to search.`)}
        </p>
        <p role="status" aria-live="polite" className="sr-only">
          {addedNote}
        </p>
        {searchSettled && !search.error && search.results.length > 0 ? (
          <ul className="divide-y divide-taupe border-y border-taupe">
            {search.results.map((result) => {
              const key = sourceKey(sourceFromResult(result));
              const added = chosenKeys.has(key);
              return (
                <li key={result.feed_url} className="flex items-center gap-3 py-3">
                  <ArtworkThumb url={result.artwork_url} title={result.title} />
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-serif text-[15px] text-navy-text">{result.title}</p>
                    {result.author ? <p className={`${HELP_TEXT_CLASS} truncate`}>{result.author}</p> : null}
                  </div>
                  <button
                    type="button"
                    onClick={() => addResult(result)}
                    disabled={added}
                    aria-label={added ? `${result.title} is already added` : `Add ${result.title}`}
                    className={SECONDARY_BUTTON_CLASS}
                  >
                    {added ? "Added" : "Add"}
                  </button>
                </li>
              );
            })}
          </ul>
        ) : null}
      </section>

      {/* Paste a link */}
      <section aria-labelledby={`${uid}-link-h`} className="space-y-3">
        <h3 id={`${uid}-link-h`} className="label-caps">
          Paste a link
        </h3>
        <form onSubmit={handleLink} className="space-y-2" noValidate>
          <label htmlFor={linkId} className="sr-only">
            Podcast link
          </label>
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              id={linkId}
              type="text"
              inputMode="url"
              value={link}
              onChange={(e) => setLink(e.target.value)}
              placeholder="https://podcasts.apple.com/…  or  https://www.youtube.com/@…"
              autoComplete="off"
              spellCheck={false}
              aria-describedby={linkHelpId}
              className={`${INPUT_CLASS} sm:flex-1`}
            />
            <button type="submit" disabled={linkBusy} className={SECONDARY_BUTTON_CLASS}>
              {linkBusy ? "Checking…" : "Add link"}
            </button>
          </div>
          <p id={linkHelpId} className={HELP_TEXT_CLASS}>
            An RSS address, an Apple Podcasts show link, or a YouTube channel link.
          </p>
        </form>
        <p
          role={linkNote?.tone === "error" ? "alert" : "status"}
          aria-live="polite"
          className={linkNote?.tone === "error" ? ERROR_TEXT_CLASS : HELP_TEXT_CLASS}
        >
          {linkNote?.text ?? ""}
        </p>
      </section>

      {/* OPML */}
      <section aria-labelledby={`${uid}-opml-h`} className="space-y-3">
        <h3 id={`${uid}-opml-h`} className="label-caps">
          Import from your podcast app
        </h3>
        <p id={fileHelpId} className={HELP_TEXT_CLASS}>
          Pocket Casts, Overcast and most independent podcast apps can export your
          subscriptions as an OPML file (a .opml or .xml file). Apple Podcasts and Spotify
          don&apos;t offer this export, so add those shows with the search above instead.
        </p>
        <div className="flex flex-wrap items-center gap-3">
          <label htmlFor={fileId} className="sr-only">
            OPML file of your podcast subscriptions
          </label>
          <input
            id={fileId}
            type="file"
            accept=".opml,.xml,text/xml,application/xml,text/x-opml"
            onChange={(e) => void handleFile(e)}
            disabled={opmlBusy}
            aria-describedby={fileHelpId}
            className={`font-sans text-xs text-ink file:mr-3 file:border file:border-navy-text file:bg-transparent file:px-3 file:py-1.5 file:font-sans file:text-xs file:uppercase file:tracking-wide file:text-navy-text hover:file:bg-navy-text hover:file:text-ivory ${FOCUS_RING_CLASS}`}
          />
          {MOCK_MODE ? (
            <button
              type="button"
              onClick={() => void handleSample()}
              disabled={opmlBusy}
              className={TEXT_BUTTON_CLASS}
            >
              Use the sample file instead
            </button>
          ) : null}
        </div>
        <div role={opmlNote ? "alert" : "status"} aria-live="polite" className="space-y-2">
          {opmlBusy ? <p className={HELP_TEXT_CLASS}>Reading your file…</p> : null}
          {opmlNote ? <p className={ERROR_TEXT_CLASS}>{opmlNote.text}</p> : null}
          {opmlSummary ? (
            <div className="space-y-2">
              <p className="font-serif text-sm text-ink">
                {opmlSummary.added === 0 ? (
                  opmlSummary.duplicates > 0 ? (
                    "Nothing new: every show in that file was already in your list"
                  ) : (
                    "That file had no podcast feeds in it"
                  )
                ) : (
                  <>
                    Imported <span className="font-mono">{opmlSummary.added}</span>{" "}
                    {opmlSummary.added === 1 ? "show" : "shows"}
                    {opmlSummary.duplicates > 0 ? (
                      <>
                        {" "}
                        (<span className="font-mono">{opmlSummary.duplicates}</span> already in your list)
                      </>
                    ) : null}
                  </>
                )}
                {opmlSummary.skipped.length > 0 ? (
                  <>
                    ;{" "}
                    <span className="font-mono">{opmlSummary.skipped.length}</span>{" "}
                    {opmlSummary.skipped.length === 1 ? "entry" : "entries"} skipped
                  </>
                ) : null}
                .
              </p>
              {opmlSummary.skipped.length > 0 ? (
                <details className="font-sans text-xs text-ink">
                  <summary className="cursor-pointer text-navy-text underline underline-offset-2">
                    Show skipped entries
                  </summary>
                  <ul className="mt-2 space-y-1">
                    {opmlSummary.skipped.map((s, i) => (
                      <li key={`${String(s.line)}-${i}`} className="text-silver">
                        <span className="font-mono text-ink">{skippedLineLabel(s.line)}</span>
                        {": "}
                        {s.reason}
                      </li>
                    ))}
                  </ul>
                </details>
              ) : null}
            </div>
          ) : null}
        </div>
      </section>

      {/* Chosen list */}
      <section aria-labelledby={`${uid}-chosen-h`} className="space-y-3">
        <h3 id={`${uid}-chosen-h`} className="label-caps">
          Your shows <span className="font-mono normal-case text-ink">({sources.length})</span>
        </h3>
        {sources.length === 0 ? (
          <p className="font-serif text-sm italic text-silver">Nothing added yet.</p>
        ) : (
          <ul className="divide-y divide-taupe border-y border-taupe">
            {sources.map((source) => {
              const title = sourceTitle(source);
              const key = sourceKey(source);
              return (
                <li key={key} className="flex items-center gap-3 py-3">
                  <ArtworkThumb url={source.kind === "rss" ? source.artwork_url : null} title={title} />
                  <div className="min-w-0 flex-1">
                    <p className="truncate font-serif text-[15px] text-navy-text">{title}</p>
                    <p className={HELP_TEXT_CLASS}>{sourceKindLabel(source)}</p>
                    {insecureFeedSources([source]).length > 0 ? (
                      <p className={ERROR_TEXT_CLASS}>
                        This feed uses http://, which Chorus cannot read. Find its https:// address.
                      </p>
                    ) : null}
                  </div>
                  <button
                    type="button"
                    onClick={() => commit(removeSource(latest.current, key))}
                    aria-label={`Remove ${title}`}
                    className={TEXT_BUTTON_CLASS}
                  >
                    Remove
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </section>
    </div>
  );
}
