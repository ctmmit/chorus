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

// --- Discovery (chorus/personas.py + chorus/discovery.py, Phase H) ---------

export const CADENCES = ["weekly", "daily", "adhoc"] as const;
export type Cadence = (typeof CADENCES)[number];

/** Mirrors chorus/personas.py Persona. */
export interface Persona {
  persona_id: string;
  name: string;
  description: string;
  soul: string;
  shows: string[];
  cadence: Cadence;
  created_at: string; // ISO 8601
  soul_version: string;
  public: boolean;
}

/** Mirrors chorus/discovery.py NetworkNode. */
export interface NetworkNode {
  id: string;
  kind: "persona" | "show";
  label: string;
  size: number;
}

/** Mirrors chorus/discovery.py NetworkEdge. */
export interface NetworkEdge {
  source: string;
  target: string;
  kind: "listens_to";
}

/** Mirrors chorus/discovery.py NetworkGraph (GET /network). */
export interface NetworkGraph {
  nodes: NetworkNode[];
  edges: NetworkEdge[];
}

// --- Episode profile (chorus/models.py EpisodeProfile) ----------------------

/** Sentinel persona: "use the request's soul as this speaker's voice". */
export const HOST_PERSONA_IS_SOUL = "the soul";

export interface SpeakerProfile {
  role: "host" | "cohost";
  name: string;
  persona: string;
  voice_id?: string | null;
}

export interface ConversationStyle {
  tone: string;
  engagement: string[];
  target_minutes: number;
}

export interface EpisodeProfile {
  name: string;
  format: "monologue" | "dialogue";
  speakers: SpeakerProfile[];
  style: ConversationStyle;
}

/** Exact mirror of chorus/models.py `TWO_HOST_PROFILE` (host = the soul;
 * cohost = a skeptical foil). Single voice is `profile: null`. */
export const TWO_HOST_PROFILE: EpisodeProfile = {
  name: "two-host",
  format: "dialogue",
  speakers: [
    { role: "host", name: "Host", persona: HOST_PERSONA_IS_SOUL, voice_id: null },
    {
      role: "cohost",
      name: "Cohost",
      persona:
        "A sharp, skeptical foil. You defend the guest's position against the " +
        "host's takes and press for specifics: whenever the host makes a claim, " +
        "ask for the number, the counterexample, or the mechanism. You are not " +
        "hostile — you are the discipline the host's opinions need.",
      voice_id: null,
    },
  ],
  style: {
    tone: "sharp, argumentative, fast-paced",
    engagement: ["interruptions", "callbacks", "disagreement", "concrete numbers"],
    target_minutes: 5,
  },
};

// --- Subscribe flow (human onboarding; /keys, /podcasts/*, /subscriptions) --

export const MIN_EPISODES_PER_RUN = 1;
export const MAX_EPISODES_PER_RUN = 20;
export const DEFAULT_HIGHLIGHT_COUNT = 4;
export const DEFAULT_EPISODES_PER_RUN = 5;

/** The six interview answer keys chorus/bootstrap.py build_from_interview
 * reads (skills/chorus-soul-bootstrap/SKILL.md). */
export const INTERVIEW_KEYS = [
  "identity",
  "interests",
  "triggers",
  "ignore",
  "style",
  "guidance",
] as const;
export type InterviewKey = (typeof INTERVIEW_KEYS)[number];

export interface RssSource {
  kind: "rss";
  feed_url: string;
  title: string | null;
  artwork_url: string | null;
}

export interface YoutubeSource {
  kind: "youtube";
  channel_id: string;
  title: string | null;
}

export interface ShowSource {
  kind: "show";
  show: string;
}

export type Source = RssSource | YoutubeSource | ShowSource;

/** GET /podcasts/search?q=&limit= */
export interface PodcastSearchResult {
  title: string;
  author: string | null;
  feed_url: string;
  artwork_url: string | null;
  apple_id: number | null;
}

/** POST /subscriptions/{id}/run: `job_id` is null when there were no new
 * episodes, and `skipped_reason` then says why (e.g. "no new episodes" or
 * "no new episodes; 1 of 3 source(s) could not be read"). */
export interface RunResult {
  job_id: string | null;
  skipped_reason: string | null;
}

/** POST /podcasts/import-opml -> sources plus the lines it could not use. */
export interface OpmlSkipped {
  line: number | string;
  reason: string;
}

export interface OpmlImportResult {
  sources: Source[];
  skipped: OpmlSkipped[];
}

export interface PreviewRequest {
  sources: Source[];
  lookback_days: number;
  max_episodes_per_run: number;
}

export interface PreviewEpisode {
  source_title: string;
  title: string;
  published_at: string; // ISO 8601
  episode: EpisodeInput;
}

export interface PreviewError {
  source: Source | string;
  reason: string;
}

export interface PreviewResponse {
  episodes: PreviewEpisode[];
  errors: PreviewError[];
}

export type SubscriptionCadence = "weekly" | "daily";

export interface LastRunSummary {
  ran_at: string; // ISO 8601
  new_episodes: number;
  job_id: string | null;
  skipped_reason: string | null;
}

export interface Subscription {
  subscription_id: string;
  owner: string;
  email: string;
  soul: string;
  context: string;
  sources: Source[] | null;
  episodes: EpisodeInput[] | null;
  shows: string[] | null;
  highlight_count: number;
  profile: EpisodeProfile | null;
  cadence: SubscriptionCadence;
  next_run_at: string; // ISO 8601
  active: boolean;
  created_at: string; // ISO 8601
  last_job_id: string | null;
  last_run_at: string | null;
  max_episodes_per_run: number;
  notify_when_empty: boolean;
  seen_episode_ids: string[];
  last_run_summary: LastRunSummary | null;
}

/** POST /subscriptions */
export interface SubscriptionCreateRequest {
  email: string;
  soul: string;
  context: string;
  sources: Source[];
  cadence: SubscriptionCadence;
  highlight_count: number;
  profile?: EpisodeProfile | null;
  max_episodes_per_run: number;
  notify_when_empty: boolean;
}

/** PATCH /subscriptions/{id} — every field optional. */
export interface SubscriptionUpdateRequest {
  context?: string;
  active?: boolean;
  cadence?: SubscriptionCadence;
  sources?: Source[];
  highlight_count?: number;
  max_episodes_per_run?: number;
  notify_when_empty?: boolean;
}
