/** Splits a freeform "explicit video ids" textarea (newline- or
 * comma-separated) into a deduplicated, trimmed list. */
export function parseVideoIds(text: string): string[] {
  const ids = text
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
  return [...new Set(ids)];
}
