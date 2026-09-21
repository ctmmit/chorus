/**
 * TypeScript mirror of chorus/models.py — the API contract (SKILL.md).
 * Field names are kept identical to the Pydantic models so the two stay
 * diffable. Do not rename fields for TS convention.
 */

// --- Request bounds (chorus/models.py) -------------------------------------

export const MAX_SOUL_CHARS = 40_000;
export const MAX_CONTEXT_CHARS = 40_000;
export const MAX_EPISODES = 25;
export const MAX_HIGHLIGHTS = 20;
export const MIN_HIGHLIGHTS = 1;

// --- Requests ----------------------------------------------------------

export interface EpisodeInput {
  url?: string | null;
  video_id?: string | null;
  show?: string | null;
  title?: string | null;
}

export interface DigestRequest {
  soul: string;
  context: string;
  episodes: EpisodeInput[];
  highlight_count: number;
  soul_origin: string;
}

export interface SelectionRequest {
  soul: string;
  context: string;
  shows?: string[] | null;
  video_ids?: string[] | null;
  highlight_count: number;
  soul_origin: string;
}

// --- Digest response -----------------------------------------------------

export interface Highlight {
  episode_id: string;
  episode_title: string | null;
  segment_timestamp: number;
  quote: string;
  relevance_score: number;
  why_surface: string;
}

/** One scored transcript window; every window is reported, surfaced or not. */
export interface WindowScore {
  start: number;
  score: number;
}

export interface EpisodeDigest {
  episode_id: string;
  episode_title: string | null;
  highlights: Highlight[];
  refused: boolean;
  refusal_reason: string | null;
  duration_seconds: number | null;
  windows: WindowScore[];
}

export interface Digest {
  soul_version: string;
  soul_origin: string;
  episodes: EpisodeDigest[];
}

/** Mirrors Digest.highlights (chorus/models.py) — a computed property in
 * Pydantic, so plain JSON never has this field; flatten it client-side. */
export function allHighlights(digest: Digest): Highlight[] {
  return digest.episodes.flatMap((ep) => ep.highlights);
}

// --- Script ----------------------------------------------------------------

export const TAKE_TYPES = [
  "idea",
  "pushback",
  "connection",
  "question",
  "cross_reference",
] as const;
export type TakeType = (typeof TAKE_TYPES)[number];

export interface Take {
  text: string;
  take_type: string;
  episode_id: string;
  segment_timestamp: number;
}

export interface Script {
  soul_version: string;
  takes: Take[];
  monologue: string;
}

// --- Job ---------------------------------------------------------------

export type JobStatus = "queued" | "digest_ready" | "done" | "failed";

export interface Job {
  job_id: string;
  status: JobStatus;
  digest: Digest | null;
  script: Script | null;
  audio_url: string | null;
  error: string | null;
  warnings: string[];
}

// --- Catalog (chorus/catalog.py, GET /shows) --------------------------------

export interface ShowEpisode {
  video_id: string;
  title: string | null;
}

export interface ShowListing {
  show: string;
  episodes: ShowEpisode[];
}
