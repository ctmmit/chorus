import type { NextConfig } from "next";

/**
 * Same-origin Chorus API proxy (docs/REVIEW_WAVE1.md #13). Off by default —
 * requires both env vars below. When enabled, every request the app makes
 * to `/api/chorus/*` is rewritten server-side to
 * `NEXT_PUBLIC_CHORUS_API_URL`, so the browser only ever talks to this
 * Next.js origin. That avoids CORS entirely: the Chorus API never needs
 * this viewer's origin listed in `CHORUS_CORS_ORIGINS`. See
 * web/README.md's "Deploying same-origin (no CORS)" for the matching
 * `NEXT_PUBLIC_CHORUS_PROXY=1` / `lib/config.ts` client-side switch that
 * points the API client at this path instead of the base URL directly.
 */
const CHORUS_PROXY_ENABLED = process.env.NEXT_PUBLIC_CHORUS_PROXY === "1";
const CHORUS_API_URL = (process.env.NEXT_PUBLIC_CHORUS_API_URL ?? "").replace(/\/+$/, "");

const nextConfig: NextConfig = {
  async rewrites() {
    if (!CHORUS_PROXY_ENABLED || !CHORUS_API_URL) return [];
    return [
      {
        source: "/api/chorus/:path*",
        destination: `${CHORUS_API_URL}/:path*`,
      },
    ];
  },
};

export default nextConfig;
