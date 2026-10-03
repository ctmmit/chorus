/**
 * Pure helpers behind the human subscribe flow (/subscribe, /subscriptions):
 * wizard-step validation, source de-duplication, fragment-token parsing,
 * date formatting, request building, and draft (de)serialization. Nothing
 * here touches the DOM, localStorage, or the network, so all of it is
 * unit-tested in subscribe.test.ts.
 */
import {
  DEFAULT_EPISODES_PER_RUN,
  DEFAULT_HIGHLIGHT_COUNT,
  INTERVIEW_KEYS,
  MAX_CONTEXT_CHARS,
  MAX_EPISODES_PER_RUN,
  MAX_HIGHLIGHTS,
  MAX_SOUL_CHARS,
  MIN_EPISODES_PER_RUN,
  MIN_HIGHLIGHTS,
  TWO_HOST_PROFILE,
  type InterviewKey,
  type PreviewEpisode,
  type PreviewError,
  type Source,
  type Subscription,
  type SubscriptionCadence,
  type SubscriptionCreateRequest,
} from "./api-types";

// --- Wizard steps -----------------------------------------------------------

export const WIZARD_STEPS = [
  { id: 1, label: "You" },
  { id: 2, label: "Shows" },
  { id: 3, label: "Your lens" },
  { id: 4, label: "Schedule & preview" },
] as const;

export type WizardStep = (typeof WIZARD_STEPS)[number]["id"];
export const FIRST_STEP: WizardStep = 1;
export const LAST_STEP: WizardStep = 4;

export type VoiceMode = "single" | "two_host";

/** Everything the wizard collects. The API token is deliberately NOT part of
 * the draft: it lives in lib/storage.ts under its own key. */
export interface SubscribeDraft {
  email: string;
  sources: Source[];
  soul: string;
  context: string;
  interview: Record<InterviewKey, string>;
  cadence: SubscriptionCadence;
  highlightCount: number;
  maxEpisodesPerRun: number;
  voice: VoiceMode;
  notifyWhenEmpty: boolean;
}

export function emptyInterview(): Record<InterviewKey, string> {
  return {
    identity: "",
    interests: "",
    triggers: "",
    ignore: "",
    style: "",
    guidance: "",
  };
}

export function defaultDraft(): SubscribeDraft {
  return {
    email: "",
    sources: [],
    soul: "",
    context: "",
    interview: emptyInterview(),
    cadence: "weekly",
    highlightCount: DEFAULT_HIGHLIGHT_COUNT,
    maxEpisodesPerRun: DEFAULT_EPISODES_PER_RUN,
    voice: "single",
    notifyWhenEmpty: false,
  };
}

// --- Sources ----------------------------------------------------------------

/** Display title of a source; falls back to the identifier when the title
 * is unknown. */
export function sourceTitle(source: Source): string {
  switch (source.kind) {
    case "rss":
      return source.title?.trim() || source.feed_url;
    case "youtube":
      return source.title?.trim() || source.channel_id;
    case "show":
      return source.show;
  }
}

export function sourceKindLabel(source: Source): string {
  switch (source.kind) {
    case "rss":
      return "Podcast feed";
    case "youtube":
      return "YouTube channel";
    case "show":
      return "Catalog show";
  }
}

/** Canonical form of a feed URL for comparison only: scheme-insensitive,
 * host lowercased, fragment and trailing slashes dropped. Unparseable input
 * is just trimmed and lowercased. */
export function normalizeFeedUrl(raw: string): string {
  const trimmed = raw.trim();
  try {
    const url = new URL(trimmed);
    const path = url.pathname.replace(/\/+$/, "");
    return `${url.host.toLowerCase()}${path}${url.search}`;
  } catch {
    return trimmed.toLowerCase();
  }
}

/** Stable identity for de-duplication: feed_url for RSS, channel_id for
 * YouTube, show name for catalog shows. */
export function sourceKey(source: Source): string {
  switch (source.kind) {
    case "rss":
      return `rss:${normalizeFeedUrl(source.feed_url)}`;
    case "youtube":
      return `youtube:${source.channel_id.trim()}`;
    case "show":
      return `show:${source.show.trim().toLowerCase()}`;
  }
}

export interface MergeResult {
  merged: Source[];
  added: number;
  duplicates: number;
}

/** Append `incoming` to `existing`, dropping anything whose key is already
 * present (including duplicates inside `incoming` itself). Order is kept. */
export function mergeSources(existing: Source[], incoming: Source[]): MergeResult {
  const seen = new Set(existing.map(sourceKey));
  const merged = [...existing];
  let added = 0;
  let duplicates = 0;
  for (const source of incoming) {
    const key = sourceKey(source);
    if (seen.has(key)) {
      duplicates += 1;
      continue;
    }
    seen.add(key);
    merged.push(source);
    added += 1;
  }
  return { merged, added, duplicates };
}

export function removeSource(sources: Source[], key: string): Source[] {
  return sources.filter((s) => sourceKey(s) !== key);
}

/** The sources a stored subscription actually covers: `sources` when the
 * server has them, otherwise any legacy catalog `shows` as show-kind sources. */
export function subscriptionSources(sub: Subscription): Source[] {
  if (sub.sources && sub.sources.length > 0) return sub.sources;
  if (sub.shows && sub.shows.length > 0) {
    return sub.shows.map((show): Source => ({ kind: "show", show }));
  }
  return [];
}

/** RSS sources whose feed address is plain `http://`. The API fetches feeds
 * over https only, so these fail at fetch time. */
export function insecureFeedSources(sources: Source[]): Source[] {
  return sources.filter((s) => s.kind === "rss" && /^http:\/\//i.test(s.feed_url.trim()));
}

/** What to tell the person after "run now": navigate to the job, or explain
 * why nothing ran (the API returns job_id null plus a skipped_reason). */
export type RunOutcome =
  | { kind: "job"; jobId: string }
  | { kind: "skipped"; reason: string };

export const DEFAULT_SKIPPED_REASON = "no new episodes";

export function interpretRun(result: { job_id: string | null; skipped_reason: string | null }): RunOutcome {
  if (result.job_id) return { kind: "job", jobId: result.job_id };
  const reason = result.skipped_reason?.trim() || DEFAULT_SKIPPED_REASON;
  return { kind: "skipped", reason };
}

/** "No digest this time: no new episodes." Sentence-cased for display. */
export function skippedRunMessage(reason: string): string {
  const trimmed = reason.trim().replace(/\.$/, "");
  return `No digest this time: ${trimmed}.`;
}

/** Up to two initials from a title, skipping a leading "The"; the fallback
 * mark when a show has no artwork. */
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

/** Largest OPML file the browser will read and send (client-side guard). */
export const MAX_OPML_BYTES = 2_000_000;

/** Search box rules: debounce and minimum query length. */
export const SEARCH_DEBOUNCE_MS = 300;
export const SEARCH_MIN_CHARS = 2;

/** "A, B, C +2 more" — a compact one-line summary of a source list. */
export function summarizeSourceTitles(sources: Source[], limit = 3): string {
  if (sources.length === 0) return "No shows";
  const titles = sources.slice(0, limit).map(sourceTitle);
  const rest = sources.length - titles.length;
  return rest > 0 ? `${titles.join(", ")} +${rest} more` : titles.join(", ");
}

// --- Validation -------------------------------------------------------------

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX_EMAIL_CHARS = 320;

export function isValidEmail(email: string): boolean {
  const trimmed = email.trim();
  return trimmed.length >= 3 && trimmed.length <= MAX_EMAIL_CHARS && EMAIL_PATTERN.test(trimmed);
}

export function clampInt(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.round(value)));
}

function isIntInRange(value: number, min: number, max: number): boolean {
  return Number.isInteger(value) && value >= min && value <= max;
}

/** Human-readable problems blocking `step`; an empty list means the step is
 * complete. `hasToken` is whether an API key is stored. */
export function validateStep(step: WizardStep, draft: SubscribeDraft, hasToken: boolean): string[] {
  const problems: string[] = [];
  switch (step) {
    case 1:
      if (!hasToken) problems.push("Sign in with the key from your email to continue.");
      break;
    case 2:
      if (draft.sources.length === 0) problems.push("Add at least one show.");
      break;
    case 3:
      if (draft.soul.trim().length === 0) {
        problems.push("Add your lens: answer the six questions or paste a soul.");
      }
      if (draft.soul.length > MAX_SOUL_CHARS) {
        problems.push(`Your soul is over the ${MAX_SOUL_CHARS.toLocaleString("en-US")} character limit.`);
      }
      if (draft.context.length > MAX_CONTEXT_CHARS) {
        problems.push(`This week's context is over the ${MAX_CONTEXT_CHARS.toLocaleString("en-US")} character limit.`);
      }
      break;
    case 4:
      if (!isValidEmail(draft.email)) problems.push("Enter the email address to send digests to.");
      if (!isIntInRange(draft.highlightCount, MIN_HIGHLIGHTS, MAX_HIGHLIGHTS)) {
        problems.push(`Highlights per episode must be ${MIN_HIGHLIGHTS} to ${MAX_HIGHLIGHTS}.`);
      }
      if (!isIntInRange(draft.maxEpisodesPerRun, MIN_EPISODES_PER_RUN, MAX_EPISODES_PER_RUN)) {
        problems.push(`Episodes per run must be ${MIN_EPISODES_PER_RUN} to ${MAX_EPISODES_PER_RUN}.`);
      }
      break;
  }
  return problems;
}

export function isStepComplete(step: WizardStep, draft: SubscribeDraft, hasToken: boolean): boolean {
  return validateStep(step, draft, hasToken).length === 0;
}

/** The furthest step the user may open: every earlier step must be complete.
 * Step 4 is reachable once steps 1 to 3 pass. */
export function furthestReachableStep(draft: SubscribeDraft, hasToken: boolean): WizardStep {
  const steps: WizardStep[] = [1, 2, 3];
  for (const step of steps) {
    if (!isStepComplete(step, draft, hasToken)) return step;
  }
  return LAST_STEP;
}

/** Clamp a requested step into what is reachable (resuming from a URL hash
 * or a saved draft must never skip an incomplete step). */
export function clampStep(requested: number, draft: SubscribeDraft, hasToken: boolean): WizardStep {
  const furthest = furthestReachableStep(draft, hasToken);
  const wanted = Math.min(Math.max(Math.trunc(requested) || FIRST_STEP, FIRST_STEP), LAST_STEP);
  return Math.min(wanted, furthest) as WizardStep;
}

export interface InterviewQuestion {
  label: string;
  hint: string | null;
  multiline: boolean;
}

/** The six questions from skills/chorus-soul-bootstrap/SKILL.md, keyed by the
 * answer keys chorus/bootstrap.py build_from_interview reads. */
export const INTERVIEW_QUESTIONS: Record<InterviewKey, InterviewQuestion> = {
  identity: {
    label: "What is your role, and what decisions are you responsible for?",
    hint: null,
    multiline: true,
  },
  interests: {
    label: "Which topics or questions are you actively pursuing?",
    hint: "Separate with commas.",
    multiline: false,
  },
  triggers: {
    label: "What would make you stop and save a podcast segment?",
    hint: "Specific ideas, claims, people, or kinds of evidence. Separate with commas.",
    multiline: false,
  },
  ignore: {
    label: "What subjects, tropes, or levels of discussion should Chorus skip?",
    hint: "Separate with commas.",
    multiline: false,
  },
  style: {
    label: "What intellectual style do you value?",
    hint: "Empirical, contrarian, technical, practical, narrative, or your own words.",
    multiline: false,
  },
  guidance: {
    label: "When should Chorus surface a segment, and when should it refuse rather than pad your digest?",
    hint: "This sets the bar. Be as strict as you want.",
    multiline: true,
  },
};

/** Interview submission needs at least one non-blank answer. */
export function interviewHasAnswers(answers: Record<string, string>): boolean {
  return INTERVIEW_KEYS.some((key) => (answers[key] ?? "").trim().length > 0);
}

/** Trim every answer and drop blanks, for POST /souls/interview. */
export function cleanInterviewAnswers(answers: Record<InterviewKey, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const key of INTERVIEW_KEYS) {
    const value = (answers[key] ?? "").trim();
    if (value) out[key] = value;
  }
  return out;
}

// --- URL fragment -----------------------------------------------------------

function fragmentParams(hash: string): URLSearchParams {
  return new URLSearchParams(hash.replace(/^#/, ""));
}

/** `#token=abc` (optionally with other params) -> "abc"; null when absent
 * or blank. The sign-in link in the key email is `<viewer>/subscribe#token=`. */
export function parseFragmentToken(hash: string): string | null {
  const token = fragmentParams(hash).get("token")?.trim();
  return token ? token : null;
}

/** `#step=3` -> 3; null when absent or out of range. */
export function parseFragmentStep(hash: string): WizardStep | null {
  const raw = fragmentParams(hash).get("step");
  if (raw === null) return null;
  const n = Number(raw);
  if (!Number.isInteger(n) || n < FIRST_STEP || n > LAST_STEP) return null;
  return n as WizardStep;
}

export function fragmentForStep(step: WizardStep): string {
  return `#step=${step}`;
}

/** Last four characters of a key, for "signed in with a key ending ...". */
export function maskToken(token: string): string {
  const trimmed = token.trim();
  return trimmed.length <= 4 ? trimmed : `…${trimmed.slice(-4)}`;
}

/** 401 and 403 mean the stored key is not (or no longer) accepted. */
export function isAuthStatus(status: number): boolean {
  return status === 401 || status === 403;
}

// --- Dates ------------------------------------------------------------------

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;
const UNKNOWN_DATE = "—";

function toDate(value: string | number | Date): Date | null {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** `02 Oct 2026` in the viewer's local time zone (Fulcrum DD MMM YYYY). */
export function formatDate(value: string | number | Date): string {
  const d = toDate(value);
  if (!d) return UNKNOWN_DATE;
  return `${pad2(d.getDate())} ${MONTHS[d.getMonth()]} ${d.getFullYear()}`;
}

/** `02 Oct 2026 14:05` in the viewer's local time zone, 24-hour clock. */
export function formatDateTime(value: string | number | Date): string {
  const d = toDate(value);
  if (!d) return UNKNOWN_DATE;
  return `${formatDate(d)} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

// --- Requests ---------------------------------------------------------------

/** Days of back-catalog the preview (and so the first digest) looks across:
 * a weekly digest covers the past week, a daily one the past day. */
export const LOOKBACK_DAYS: Record<SubscriptionCadence, number> = { weekly: 7, daily: 1 };

export function lookbackDaysFor(cadence: SubscriptionCadence): number {
  return LOOKBACK_DAYS[cadence];
}

export const CADENCE_LABELS: Record<SubscriptionCadence, string> = {
  weekly: "Weekly, on Friday",
  daily: "Daily",
};

export function profileForVoice(voice: VoiceMode) {
  return voice === "two_host" ? TWO_HOST_PROFILE : null;
}

/** The POST /subscriptions body for a completed draft. Single voice is an
 * explicit `profile: null` (the monologue default). */
export function buildSubscriptionRequest(draft: SubscribeDraft): SubscriptionCreateRequest {
  return {
    email: draft.email.trim(),
    soul: draft.soul,
    context: draft.context,
    sources: draft.sources,
    cadence: draft.cadence,
    highlight_count: draft.highlightCount,
    profile: profileForVoice(draft.voice),
    max_episodes_per_run: draft.maxEpisodesPerRun,
    notify_when_empty: draft.notifyWhenEmpty,
  };
}

export interface PreviewGroup {
  source_title: string;
  episodes: PreviewEpisode[];
}

/** Group preview episodes by show, in order of first appearance, newest
 * episode first within each group. */
export function groupPreviewBySource(episodes: PreviewEpisode[]): PreviewGroup[] {
  const groups = new Map<string, PreviewEpisode[]>();
  for (const ep of episodes) {
    const list = groups.get(ep.source_title);
    if (list) list.push(ep);
    else groups.set(ep.source_title, [ep]);
  }
  return [...groups.entries()].map(([source_title, list]) => ({
    source_title,
    episodes: [...list].sort(
      (a, b) => (toDate(b.published_at)?.getTime() ?? 0) - (toDate(a.published_at)?.getTime() ?? 0),
    ),
  }));
}

/** A preview error's source as a label (the API may send a Source or text). */
export function previewErrorLabel(error: PreviewError): string {
  return typeof error.source === "string" ? error.source : sourceTitle(error.source);
}

/** "line 12" for numeric lines, the raw text otherwise. */
export function skippedLineLabel(line: number | string): string {
  return typeof line === "number" ? `Line ${line}` : String(line);
}

export type LastRunView =
  | { kind: "none" }
  | { kind: "ran"; ranAt: string; newEpisodes: number; jobId: string | null }
  | { kind: "skipped"; ranAt: string; reason: string };

/** How to describe a subscription's most recent run. Prefers
 * `last_run_summary`; falls back to the bare `last_run_at`/`last_job_id`. */
export function describeLastRun(sub: Subscription): LastRunView {
  const summary = sub.last_run_summary;
  if (summary) {
    if (summary.skipped_reason) {
      return { kind: "skipped", ranAt: summary.ran_at, reason: summary.skipped_reason };
    }
    return {
      kind: "ran",
      ranAt: summary.ran_at,
      newEpisodes: summary.new_episodes,
      jobId: summary.job_id ?? sub.last_job_id,
    };
  }
  if (sub.last_run_at) {
    return { kind: "ran", ranAt: sub.last_run_at, newEpisodes: 0, jobId: sub.last_job_id };
  }
  return { kind: "none" };
}

// --- Draft persistence ------------------------------------------------------

export function serializeDraft(draft: SubscribeDraft): string {
  return JSON.stringify(draft);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isString(value: unknown): value is string {
  return typeof value === "string";
}

export function isSource(value: unknown): value is Source {
  if (!isRecord(value)) return false;
  switch (value.kind) {
    case "rss":
      return isString(value.feed_url) && value.feed_url.length > 0;
    case "youtube":
      return isString(value.channel_id) && value.channel_id.length > 0;
    case "show":
      return isString(value.show) && value.show.length > 0;
    default:
      return false;
  }
}

/** Rebuild a draft from saved JSON, field by field, so a stale or tampered
 * entry degrades to defaults instead of crashing the wizard. */
export function parseDraft(raw: string | null): SubscribeDraft {
  const draft = defaultDraft();
  if (!raw) return draft;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return draft;
  }
  if (!isRecord(parsed)) return draft;

  if (isString(parsed.email)) draft.email = parsed.email;
  if (Array.isArray(parsed.sources)) draft.sources = mergeSources([], parsed.sources.filter(isSource)).merged;
  if (isString(parsed.soul)) draft.soul = parsed.soul;
  if (isString(parsed.context)) draft.context = parsed.context;
  if (isRecord(parsed.interview)) {
    for (const key of INTERVIEW_KEYS) {
      const value = parsed.interview[key];
      if (isString(value)) draft.interview[key] = value;
    }
  }
  if (parsed.cadence === "weekly" || parsed.cadence === "daily") draft.cadence = parsed.cadence;
  if (typeof parsed.highlightCount === "number") {
    draft.highlightCount = clampInt(parsed.highlightCount, MIN_HIGHLIGHTS, MAX_HIGHLIGHTS);
  }
  if (typeof parsed.maxEpisodesPerRun === "number") {
    draft.maxEpisodesPerRun = clampInt(parsed.maxEpisodesPerRun, MIN_EPISODES_PER_RUN, MAX_EPISODES_PER_RUN);
  }
  if (parsed.voice === "single" || parsed.voice === "two_host") draft.voice = parsed.voice;
  if (typeof parsed.notifyWhenEmpty === "boolean") draft.notifyWhenEmpty = parsed.notifyWhenEmpty;
  return draft;
}
