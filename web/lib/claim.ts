import { allHighlights, type Digest } from "./api-types";

/**
 * The header claim-title (spec: "4 of 6 episodes cleared the bar; 11
 * highlights") — a Kazakoff-style claim derived from the data, not a
 * generic "Your digest" label. `skippedCount` is `usage.skipped.length`
 * (Phase C): the server's own count of episodes it could not resolve a
 * transcript for, so the total reflects everything actually requested,
 * not just what made it into the digest.
 */
export function buildClaimTitle(digest: Digest, skippedCount = 0): string {
  const cleared = digest.episodes.filter((e) => !e.refused).length;
  const total = digest.episodes.length + skippedCount;
  const highlightCount = allHighlights(digest).length;
  const episodeWord = total === 1 ? "episode" : "episodes";
  const highlightWord = highlightCount === 1 ? "highlight" : "highlights";
  return `${cleared} of ${total} ${episodeWord} cleared the bar; ${highlightCount} ${highlightWord}`;
}
