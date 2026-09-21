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
  // RSS / Podcasting 2.0 identification (Phase C): a podcast feed plus the
  // <guid> of one <item>; audio_url may be supplied directly or resolved
  // from the feed's <enclosure>.
  feed_url?: string | null;
  guid?: string | null;
  audio_url?: string | null;
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

// --- Usage (Phase C run telemetry — chorus/models.py JobUsage) -------------

export interface SkippedEpisode {
  episode: EpisodeInput;
  reason: string;
}

/** Token counts for one job, split so the prompt-cache hit rate is visible:
 * a healthy batched run has cache_read >> input after the first call. */
export interface LLMTokens {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
}

/** Run telemetry — not part of the lifecycle contract, safe to ignore, but
 * the cost/observability meter a caller (or a human) can read. */
export interface JobUsage {
  // Wall-clock seconds per pipeline stage: "ingest" | "curate" | "script" | "audio".
  stage_seconds: Record<string, number>;
  // resolved episode id -> which provider produced it ("fixture", "supadata",
  // "rss:json"/"rss:vtt"/"rss:srt", "deepgram").
  transcript_sources: Record<string, string>;
  // Episodes ingest() could not resolve a transcript for, and why.
  skipped: SkippedEpisode[];
  llm_tokens: LLMTokens | null;
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
  usage: JobUsage | null;
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
