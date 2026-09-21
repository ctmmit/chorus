import { allHighlights, type Digest } from "./api-types";

/**
 * The header claim-title (spec: "4 of 6 episodes cleared the bar; 11
 * highlights") — a Kazakoff-style claim derived from the data, not a
 * generic "Your digest" label. `requestedTotal` is the count of episodes
 * the caller originally asked for (known client-side from the submitted
 * request); when unknown we fall back to the digest's own episode count,
 * which undercounts silently-skipped episodes.
 */
export function buildClaimTitle(digest: Digest, requestedTotal?: number): string {
  const cleared = digest.episodes.filter((e) => !e.refused).length;
  // Guard against requestedTotal undercounting the digest's own episode
  // list (shouldn't happen against a real API, but mock mode's canned
  // fixtures don't always match what was actually requested).
  const total = Math.max(requestedTotal ?? digest.episodes.length, digest.episodes.length);
  const highlightCount = allHighlights(digest).length;
  const episodeWord = total === 1 ? "episode" : "episodes";
  const highlightWord = highlightCount === 1 ? "highlight" : "highlights";
  return `${cleared} of ${total} ${episodeWord} cleared the bar; ${highlightCount} ${highlightWord}`;
}
