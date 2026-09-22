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

/**
 * True when `url` is relative (matches `absoluteUrl`'s own test for "not
 * already absolute" — it will be resolved against `apiBaseUrl`, so it always
 * lands on the configured API origin) or is an `http(s)://` URL whose origin
 * exactly equals `apiBaseUrl`'s origin. Used to decide whether the Chorus
 * bearer token is safe to attach to a request: never send it to a
 * third-party host such as a blob storage origin (see
 * docs/REVIEW_WAVE1.md #12).
 *
 * A malformed `url` or `apiBaseUrl` (unparseable by the WHATWG URL parser)
 * is treated as NOT same-origin — fail closed, never attach the token.
 */
export function isSameOriginOrRelative(url: string, apiBaseUrl: string): boolean {
  if (!/^https?:\/\//i.test(url)) return true;
  try {
    return new URL(url).origin === new URL(apiBaseUrl).origin;
  } catch {
    return false;
  }
}
