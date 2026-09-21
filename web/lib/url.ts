/** Small pure URL helpers shared by the API client and audio player. */

export function trimTrailingSlash(url: string): string {
  return url.endsWith("/") ? url.slice(0, -1) : url;
}

/** Resolve a path Chorus returns (e.g. `audio_url: "/artifacts/x.mp3"`)
 * against the configured base URL. Already-absolute URLs pass through. */
export function absoluteUrl(baseUrl: string, path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  const base = trimTrailingSlash(baseUrl);
  return path.startsWith("/") ? `${base}${path}` : `${base}/${path}`;
}
