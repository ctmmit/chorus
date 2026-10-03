/**
 * Pure logic behind the mock-mode subscribe endpoints (/api/mock/podcasts/*,
 * /api/mock/souls/interview, /api/mock/subscriptions/*). No filesystem, no
 * module state, so it is unit-tested directly; the route handlers wire it to
 * web/mocks/ fixtures and the in-memory store (mock-store.ts).
 *
 * This stands in for the real backend only far enough that the wizard is
 * clickable end to end. It is not a second implementation of the contract:
 * the real resolver fetches feeds, the real preview reads them.
 */
import type {
  EpisodeInput,
  OpmlImportResult,
  PodcastSearchResult,
  PreviewError,
  PreviewResponse,
  PreviewEpisode,
  Source,
  Subscription,
  SubscriptionCadence,
  SubscriptionCreateRequest,
  SubscriptionUpdateRequest,
} from "@/lib/api-types";
import { MAX_CONTEXT_CHARS, MAX_SOUL_CHARS } from "@/lib/api-types";
import { isSource, isValidEmail, mergeSources, sourceTitle } from "@/lib/subscribe";

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;
/** Mock digests run at 12:00 UTC (the real scheduler's hour is the backend's). */
const RUN_HOUR_UTC = 12;
const FRIDAY = 5;

// --- Search -----------------------------------------------------------------

export const DEFAULT_SEARCH_LIMIT = 10;

/** Case-insensitive title/author match; titles that start with the query
 * rank first, then other title matches, then author matches. */
export function searchCatalog(
  catalog: PodcastSearchResult[],
  query: string,
  limit: number = DEFAULT_SEARCH_LIMIT,
): PodcastSearchResult[] {
  const q = query.trim().toLowerCase();
  if (!q) return [];
  const scored: Array<{ rank: number; result: PodcastSearchResult }> = [];
  for (const result of catalog) {
    const title = result.title.toLowerCase();
    const author = (result.author ?? "").toLowerCase();
    let rank: number | null = null;
    if (title.startsWith(q)) rank = 0;
    else if (title.includes(q)) rank = 1;
    else if (author.includes(q)) rank = 2;
    if (rank !== null) scored.push({ rank, result });
  }
  scored.sort((a, b) => a.rank - b.rank || a.result.title.localeCompare(b.result.title));
  return scored.slice(0, Math.max(0, limit)).map((s) => s.result);
}

// --- Resolve ----------------------------------------------------------------

export type ResolveOutcome = { ok: true; source: Source } | { ok: false; detail: string };

const RSS_HINTS = [/\.(rss|xml|atom)(\?|$)/i, /\/(feed|feeds|rss|podcast)(\/|$|\?)/i, /^feeds?\./i, /^rss\./i];

function titleFromSlug(slug: string): string {
  const words = decodeURIComponent(slug)
    .split(/[-_]+/)
    .filter(Boolean)
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1));
  return words.join(" ") || "Untitled podcast";
}

/** Deterministic 32-bit string hash (FNV-1a) used to fake stable ids. */
export function hashString(value: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

const ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-";

/** A stable pseudo-random id of `length` characters derived from `seed`. */
export function fakeId(seed: string, length: number): string {
  let state = hashString(seed) || 1;
  let out = "";
  for (let i = 0; i < length; i += 1) {
    state = (Math.imul(state, 1664525) + 1013904223) >>> 0;
    out += ID_ALPHABET[(state >>> 24) % ID_ALPHABET.length];
  }
  return out;
}

/** Recognize the three link forms the real POST /podcasts/resolve accepts
 * (RSS URL, Apple Podcasts show URL, YouTube channel URL). */
export function resolveLink(raw: string, catalog: PodcastSearchResult[]): ResolveOutcome {
  const text = raw.trim();
  if (!text) return { ok: false, detail: "Paste a link first." };

  let url: URL;
  try {
    url = new URL(/^[a-z][a-z0-9+.-]*:\/\//i.test(text) ? text : `https://${text}`);
  } catch {
    return { ok: false, detail: "That is not a valid link. Paste the full address, starting with https://." };
  }
  if (url.protocol !== "http:" && url.protocol !== "https:") {
    return { ok: false, detail: "Only http and https links are supported." };
  }

  const host = url.hostname.toLowerCase().replace(/^www\./, "");

  if (host === "podcasts.apple.com" || host === "itunes.apple.com") {
    const idMatch = url.pathname.match(/\/id(\d{5,})/);
    if (!idMatch) {
      return {
        ok: false,
        detail: "That Apple Podcasts link has no show id. Open the show's page and copy its link.",
      };
    }
    const appleId = idMatch[1];
    const known = catalog.find((c) => String(c.apple_id) === appleId);
    if (known) {
      return {
        ok: true,
        source: { kind: "rss", feed_url: known.feed_url, title: known.title, artwork_url: known.artwork_url },
      };
    }
    const slug = url.pathname.split("/").find((part, i, all) => all[i + 1]?.startsWith("id") && part) ?? "";
    const title = titleFromSlug(slug);
    return {
      ok: true,
      source: {
        kind: "rss",
        feed_url: `https://feeds.example.com/apple/${appleId}.xml`,
        title,
        artwork_url: `/api/mock/artwork?t=${encodeURIComponent(title)}`,
      },
    };
  }

  if (host === "youtube.com" || host === "m.youtube.com" || host === "youtu.be") {
    const channelMatch = url.pathname.match(/^\/channel\/(UC[\w-]{20,})/);
    if (channelMatch) {
      return { ok: true, source: { kind: "youtube", channel_id: channelMatch[1], title: null } };
    }
    const handleMatch = url.pathname.match(/^\/(@[\w.-]+)/) ?? url.pathname.match(/^\/(?:c|user)\/([\w.-]+)/);
    if (handleMatch) {
      const handle = handleMatch[1];
      return {
        ok: true,
        source: { kind: "youtube", channel_id: `UC${fakeId(handle.toLowerCase(), 22)}`, title: handle },
      };
    }
    return {
      ok: false,
      detail: "That YouTube link is not a channel. Use a link like youtube.com/@name or youtube.com/channel/UC...",
    };
  }

  const known = catalog.find((c) => {
    try {
      return new URL(c.feed_url).host === url.host && new URL(c.feed_url).pathname === url.pathname;
    } catch {
      return false;
    }
  });
  if (known) {
    return {
      ok: true,
      source: { kind: "rss", feed_url: known.feed_url, title: known.title, artwork_url: known.artwork_url },
    };
  }

  if (RSS_HINTS.some((hint) => hint.test(url.pathname) || hint.test(url.hostname))) {
    const lastSegment = url.pathname.split("/").filter(Boolean).pop() ?? "";
    const title = titleFromSlug(lastSegment.replace(/\.(rss|xml|atom)$/i, "")) || url.hostname;
    return {
      ok: true,
      source: {
        kind: "rss",
        feed_url: url.toString(),
        title: lastSegment ? title : host,
        artwork_url: null,
      },
    };
  }

  return {
    ok: false,
    detail:
      "That link is not an RSS feed, an Apple Podcasts show, or a YouTube channel. " +
      "Try the show's RSS address, or search by name above.",
  };
}

// --- OPML -------------------------------------------------------------------

const XML_ENTITIES: Record<string, string> = {
  amp: "&",
  lt: "<",
  gt: ">",
  quot: '"',
  apos: "'",
};

export function decodeXmlEntities(value: string): string {
  return value.replace(/&(#x[0-9a-f]+|#\d+|[a-z]+);/gi, (whole, body: string) => {
    if (body.startsWith("#x") || body.startsWith("#X")) {
      return String.fromCodePoint(parseInt(body.slice(2), 16));
    }
    if (body.startsWith("#")) return String.fromCodePoint(parseInt(body.slice(1), 10));
    return XML_ENTITIES[body.toLowerCase()] ?? whole;
  });
}

function parseAttributes(raw: string): Record<string, string> {
  const attrs: Record<string, string> = {};
  const pattern = /([\w:.-]+)\s*=\s*(?:"([^"]*)"|'([^']*)')/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(raw)) !== null) {
    attrs[match[1].toLowerCase()] = decodeXmlEntities(match[2] ?? match[3] ?? "");
  }
  return attrs;
}

export function looksLikeOpml(text: string): boolean {
  return /<opml[\s>]/i.test(text) && /<outline[\s/>]/i.test(text);
}

/** Pull podcast feeds out of an OPML export. Container outlines (folders,
 * which have children) are traversed silently; a self-closing outline with
 * no usable feed URL is reported in `skipped` with its 1-based line number. */
export function parseOpml(opml: string): OpmlImportResult {
  const sources: Source[] = [];
  const skipped: OpmlImportResult["skipped"] = [];
  const pattern = /<outline\b((?:[^>"']|"[^"]*"|'[^']*')*?)(\/?)>/gi;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(opml)) !== null) {
    const attrs = parseAttributes(match[1]);
    const selfClosing = match[2] === "/";
    const line = opml.slice(0, match.index).split("\n").length;
    const feed = attrs.xmlurl?.trim();
    const title = (attrs.title || attrs.text || "").trim() || null;

    if (!feed) {
      if (selfClosing) {
        skipped.push({ line, reason: `No feed address (xmlUrl) for "${title ?? "untitled entry"}".` });
      }
      continue;
    }
    if (!/^https?:\/\//i.test(feed)) {
      skipped.push({ line, reason: `"${title ?? feed}" uses a feed address that is not http or https.` });
      continue;
    }
    sources.push({ kind: "rss", feed_url: feed, title, artwork_url: null });
  }
  return { sources, skipped };
}

// --- Interview --------------------------------------------------------------

function bullets(items: string[]): string {
  return items.length > 0 ? items.map((i) => `- ${i}`).join("\n") : "- (none inferred)";
}

function splitList(value: string | undefined): string[] {
  return (value ?? "")
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Port of chorus/bootstrap.py MockSoulBuilder.build_from_interview — the
 * same six-section template, filled from the six answer keys. */
export function renderSoulFromInterview(answers: Record<string, string>): string {
  const pick = (key: string, fallback: string): string => answers[key]?.trim() || fallback;
  const interests = splitList(answers.interests);
  const triggers = splitList(answers.triggers);
  return [
    "# Soul (interview)",
    "",
    "## Identity & Role",
    pick("identity", "Stated by the principal."),
    "",
    "## Core Interests",
    bullets(interests),
    "",
    "## Attention Triggers",
    bullets(triggers.length > 0 ? triggers : interests),
    "",
    "## Anti-interests",
    bullets(splitList(answers.ignore)),
    "",
    "## Taste & Sensibility",
    pick("style", "As stated."),
    "",
    "## Curation Guidance",
    pick("guidance", "Surface segments matching the triggers; high bar for everything else."),
    "",
  ].join("\n");
}

// --- Preview ----------------------------------------------------------------

const EPISODE_TOPICS = [
  "The unit economics of AI inference",
  "Inside a private-credit unwind",
  "Why pricing power beats growth",
  "What the yield curve is telling you",
  "Capital allocation after the buyback boom",
  "How a regional bank actually fails",
  "The case against index concentration",
  "Reading a 10-K backwards",
  "Moats in marketplaces",
  "Founder succession and the family office",
  "Energy demand from data centers",
  "A short history of operating leverage",
];

export function sourceIdentity(source: Source): string {
  switch (source.kind) {
    case "rss":
      return source.feed_url;
    case "youtube":
      return source.channel_id;
    case "show":
      return source.show;
  }
}

function episodeFor(source: Source, title: string, guidSeed: string): EpisodeInput {
  const show = sourceTitle(source);
  switch (source.kind) {
    case "rss":
      return { feed_url: source.feed_url, guid: fakeId(guidSeed, 16), title, show };
    case "youtube":
      return { video_id: fakeId(guidSeed, 11), title, show };
    case "show":
      return { show, title };
  }
}

/** A week (or `lookbackDays`) of plausible episodes per source, newest
 * first, capped at `maxEpisodes` overall. Sources whose address contains
 * "unreachable" produce a source error instead, so the UI's error path is
 * exercisable. Deterministic for a given `now`. */
export function buildMockPreview(
  sources: Source[],
  lookbackDays: number,
  maxEpisodes: number,
  now: Date,
): PreviewResponse {
  const episodes: PreviewEpisode[] = [];
  const errors: PreviewError[] = [];
  const windowMs = Math.max(1, lookbackDays) * DAY_MS;

  for (const source of sources) {
    const identity = sourceIdentity(source);
    if (/unreachable/i.test(identity)) {
      errors.push({ source, reason: "The feed did not respond (HTTP 504)." });
      continue;
    }
    const h = hashString(identity);
    const count = lookbackDays <= 1 ? h % 2 : 1 + (h % 3);
    for (let i = 0; i < count; i += 1) {
      const topic = EPISODE_TOPICS[(h + i * 5) % EPISODE_TOPICS.length];
      const age = Math.floor(((i + 1) / (count + 1)) * windowMs) + ((h >>> (i * 3)) % HOUR_MS);
      const number = 100 + ((h >>> 8) % 400) + (count - i);
      const title = `${number}. ${topic}`;
      episodes.push({
        source_title: sourceTitle(source),
        title,
        published_at: new Date(now.getTime() - age).toISOString(),
        episode: episodeFor(source, title, `${identity}#${i}`),
      });
    }
  }

  episodes.sort((a, b) => Date.parse(b.published_at) - Date.parse(a.published_at));
  return { episodes: episodes.slice(0, Math.max(0, maxEpisodes)), errors };
}

// --- Subscriptions ----------------------------------------------------------

/** The next scheduled run strictly after `from`: weekly = the next Friday at
 * 12:00 UTC, daily = the next 12:00 UTC. */
export function nextRunAt(cadence: SubscriptionCadence, from: Date): Date {
  const candidate = new Date(
    Date.UTC(from.getUTCFullYear(), from.getUTCMonth(), from.getUTCDate(), RUN_HOUR_UTC, 0, 0, 0),
  );
  if (cadence === "daily") {
    if (candidate.getTime() <= from.getTime()) candidate.setUTCDate(candidate.getUTCDate() + 1);
    return candidate;
  }
  const daysUntilFriday = (FRIDAY - candidate.getUTCDay() + 7) % 7;
  candidate.setUTCDate(candidate.getUTCDate() + daysUntilFriday);
  if (candidate.getTime() <= from.getTime()) candidate.setUTCDate(candidate.getUTCDate() + 7);
  return candidate;
}

/** A human-readable problem with a create request, or null when it is valid
 * (the real API answers 422 with `{detail}`; the mock does the same). */
export function validateCreateRequest(body: Partial<SubscriptionCreateRequest>): string | null {
  if (typeof body.email !== "string" || !isValidEmail(body.email)) return "email must be a valid address";
  if (typeof body.soul !== "string" || body.soul.trim().length === 0) return "soul is required";
  if (body.soul.length > MAX_SOUL_CHARS) return "soul is too long";
  if (typeof body.context === "string" && body.context.length > MAX_CONTEXT_CHARS) return "context is too long";
  if (!Array.isArray(body.sources) || body.sources.length === 0 || !body.sources.every(isSource)) {
    return "at least one valid source is required";
  }
  if (body.cadence !== "weekly" && body.cadence !== "daily") return 'cadence must be "weekly" or "daily"';
  return null;
}

export function subscriptionFromRequest(
  body: SubscriptionCreateRequest,
  subscriptionId: string,
  owner: string,
  now: Date,
): Subscription {
  return {
    subscription_id: subscriptionId,
    owner,
    email: body.email.trim(),
    soul: body.soul,
    context: body.context ?? "",
    sources: mergeSources([], body.sources).merged,
    episodes: null,
    shows: null,
    highlight_count: body.highlight_count,
    profile: body.profile ?? null,
    cadence: body.cadence,
    next_run_at: nextRunAt(body.cadence, now).toISOString(),
    active: true,
    created_at: now.toISOString(),
    last_job_id: null,
    last_run_at: null,
    max_episodes_per_run: body.max_episodes_per_run,
    notify_when_empty: body.notify_when_empty,
    seen_episode_ids: [],
    last_run_summary: null,
  };
}

/** Apply a PATCH. A cadence change or a resume re-plans `next_run_at`. */
export function applyUpdate(sub: Subscription, patch: SubscriptionUpdateRequest, now: Date): Subscription {
  const next: Subscription = { ...sub };
  if (patch.context !== undefined) next.context = patch.context;
  if (patch.highlight_count !== undefined) next.highlight_count = patch.highlight_count;
  if (patch.max_episodes_per_run !== undefined) next.max_episodes_per_run = patch.max_episodes_per_run;
  if (patch.notify_when_empty !== undefined) next.notify_when_empty = patch.notify_when_empty;
  if (patch.sources !== undefined) {
    next.sources = mergeSources([], patch.sources).merged;
    next.shows = null;
  }
  const resumed = patch.active === true && !sub.active;
  if (patch.active !== undefined) next.active = patch.active;
  if (patch.cadence !== undefined) next.cadence = patch.cadence;
  if (resumed || (patch.cadence !== undefined && patch.cadence !== sub.cadence)) {
    next.next_run_at = nextRunAt(next.cadence, now).toISOString();
  }
  return next;
}

/** Record a manual "run now": a job id, the episodes it covered, and the
 * following scheduled run. */
export function applyRun(sub: Subscription, jobId: string, newEpisodes: number, now: Date): Subscription {
  return {
    ...sub,
    last_job_id: jobId,
    last_run_at: now.toISOString(),
    next_run_at: nextRunAt(sub.cadence, now).toISOString(),
    last_run_summary: {
      ran_at: now.toISOString(),
      new_episodes: newEpisodes,
      job_id: jobId,
      skipped_reason: null,
    },
  };
}

// --- Artwork placeholder ------------------------------------------------------

function escapeXml(value: string): string {
  return value.replace(/[<>&"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

/** Up to two initials from a title, skipping a leading "The". */
export function initialsFor(title: string): string {
  const words = title
    .split(/\s+/)
    .filter(Boolean)
    .filter((w, i) => !(i === 0 && /^the$/i.test(w)));
  const letters = words
    .slice(0, 2)
    .map((w) => w.charAt(0).toUpperCase())
    .join("");
  return letters || "?";
}

/** A flat navy square with the show's initials: stands in for cover art in
 * mock mode so the UI never hot-links third-party images. */
export function placeholderArtworkSvg(title: string): string {
  const initials = escapeXml(initialsFor(title));
  return (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 96 96" width="96" height="96">' +
    '<rect width="96" height="96" fill="#0F2340"/>' +
    '<text x="48" y="58" text-anchor="middle" font-family="Georgia, serif" font-size="34" fill="#F6F1E7">' +
    `${initials}</text></svg>`
  );
}
