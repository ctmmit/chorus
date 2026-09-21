/**
 * Pure scale/format helpers for the episode timeline strip (the centerpiece
 * of /jobs/[id] — DEVELOPMENT_PLAN §6.1). Kept side-effect-free and framework
 * -free so they are unit-testable with Vitest and reusable from /compare.
 */
import type { EpisodeDigest, Highlight, WindowScore } from "./api-types";

/** A YouTube video id is exactly 11 chars of [A-Za-z0-9_-]. Deep links only
 * make sense for ids that look like real YouTube ids. */
const YOUTUBE_ID_RE = /^[A-Za-z0-9_-]{11}$/;

export function isYouTubeVideoId(id: string): boolean {
  return YOUTUBE_ID_RE.test(id);
}

/** `https://www.youtube.com/watch?v=<id>&t=<int seconds>s`, or null when the
 * id does not look like a YouTube id (per spec: only link when it does). */
export function youtubeDeepLink(episodeId: string, seconds: number): string | null {
  if (!isYouTubeVideoId(episodeId)) return null;
  const t = Math.max(0, Math.round(seconds));
  return `https://www.youtube.com/watch?v=${episodeId}&t=${t}s`;
}

/** mm:ss for anything under an hour, h:mm:ss beyond that. Negative or
 * non-finite input clamps to 0:00. */
export function secondsToClock(totalSeconds: number): string {
  const s = Number.isFinite(totalSeconds) ? Math.max(0, Math.round(totalSeconds)) : 0;
  const hours = Math.floor(s / 3600);
  const minutes = Math.floor((s % 3600) / 60);
  const seconds = s % 60;
  const mm = hours > 0 ? String(minutes).padStart(2, "0") : String(minutes);
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

/** Best-effort episode duration in seconds: prefer the server-reported
 * duration_seconds, else the latest window/highlight start plus a pad. */
export function resolveDurationSeconds(episode: {
  duration_seconds: number | null;
  windows: WindowScore[];
  highlights: Highlight[];
}): number {
  if (episode.duration_seconds != null && episode.duration_seconds > 0) {
    return episode.duration_seconds;
  }
  const starts = [
    ...episode.windows.map((w) => w.start),
    ...episode.highlights.map((h) => h.segment_timestamp),
  ];
  if (starts.length === 0) return 0;
  return Math.max(...starts) + 30;
}

/** x position (px) for a timestamp along a bar of the given pixel width. */
export function xForTime(time: number, durationSeconds: number, width: number): number {
  if (durationSeconds <= 0) return 0;
  const clamped = Math.min(Math.max(time, 0), durationSeconds);
  return (clamped / durationSeconds) * width;
}

/** Tick pixel height for a 0..1 relevance score, clamped into [minHeight,
 * maxHeight] so a score of 0 still renders a visible (if minimal) tick. */
export function tickHeightForScore(
  score: number,
  maxHeight: number,
  minHeight = 2,
): number {
  const clamped = Math.min(Math.max(score, 0), 1);
  return minHeight + clamped * (maxHeight - minHeight);
}

const DEFAULT_MATCH_TOLERANCE_SECONDS = 1;

/** Whether a window's start timestamp corresponds to a surfaced highlight
 * (matched within a tolerance, since scored windows and highlight
 * timestamps are not always bit-identical floats of the same value). */
export function isHighlightWindow(
  windowStart: number,
  highlightTimestamps: number[],
  toleranceSeconds = DEFAULT_MATCH_TOLERANCE_SECONDS,
): boolean {
  return highlightTimestamps.some((t) => Math.abs(t - windowStart) <= toleranceSeconds);
}

export function highlightTimestamps(episode: EpisodeDigest): number[] {
  return episode.highlights.map((h) => h.segment_timestamp);
}

/**
 * Overlap between two sets of highlight timestamps for the *same* episode,
 * used by /compare's soul-divergence view. Timestamps are rounded to the
 * nearest second before comparing (spec: "overlap percentage by rounded
 * timestamp"). Returns a 0..100 Jaccard-style overlap: shared / union.
 * Two empty sets are defined as 0% overlap (nothing to agree on).
 */
export function overlapPercentage(
  aTimestamps: number[],
  bTimestamps: number[],
  toleranceSeconds = DEFAULT_MATCH_TOLERANCE_SECONDS,
): number {
  const a = new Set(aTimestamps.map((t) => Math.round(t / toleranceSeconds)));
  const b = new Set(bTimestamps.map((t) => Math.round(t / toleranceSeconds)));
  if (a.size === 0 && b.size === 0) return 0;
  let shared = 0;
  for (const t of a) {
    if (b.has(t)) shared += 1;
  }
  const union = new Set([...a, ...b]).size;
  return union === 0 ? 0 : (shared / union) * 100;
}
