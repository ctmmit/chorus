/** Pure helpers for Phase C run telemetry (chorus/models.py JobUsage). */
import type { EpisodeInput, LLMTokens } from "./api-types";

const KNOWN_STAGE_ORDER = ["ingest", "curate", "script", "audio"];

/** stage_seconds in a stable, pipeline-order sequence (ingest -> curate ->
 * script -> audio), with any unrecognized stage keys appended after, in
 * whatever order they appear in the object. */
export function orderedStages(stageSeconds: Record<string, number>): [string, number][] {
  const known = KNOWN_STAGE_ORDER.filter((s) => s in stageSeconds).map(
    (s): [string, number] => [s, stageSeconds[s]],
  );
  const rest = Object.entries(stageSeconds).filter(([s]) => !KNOWN_STAGE_ORDER.includes(s));
  return [...known, ...rest];
}

/** Cache-read share of (input + cache_read) tokens, as 0..100. Null when
 * there is nothing to compute a share from — an explicit "no data" rather
 * than a misleading 0%. */
export function cacheReadSharePercent(tokens: LLMTokens): number | null {
  const denominator = tokens.input_tokens + tokens.cache_read_tokens;
  if (denominator <= 0) return null;
  return (tokens.cache_read_tokens / denominator) * 100;
}

/** Best-effort display id for a (possibly RSS-sourced) EpisodeInput in a
 * skipped-episode list: prefer the YouTube video id, then title, then the
 * RSS guid, then the feed URL — whatever the caller actually supplied. */
export function episodeInputLabel(episode: EpisodeInput): string {
  return (
    episode.video_id ??
    episode.title ??
    episode.guid ??
    episode.feed_url ??
    episode.url ??
    "unknown episode"
  );
}
