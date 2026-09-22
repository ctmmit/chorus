/** Mock mode serves web/mocks/*.json through the /api/mock route handlers
 * instead of calling a real Chorus API, so the UI runs with no backend. */
export const MOCK_MODE = process.env.NEXT_PUBLIC_CHORUS_MOCK === "1";

/** Default base URL for a real deployment; the connect form on `/` can
 * override this per-browser (persisted to localStorage). */
export const DEFAULT_API_BASE_URL = process.env.NEXT_PUBLIC_CHORUS_API_URL ?? "";

/** When `1`, the browser calls the Chorus API through this Next.js origin
 * (`CHORUS_PROXY_BASE_PATH`) instead of the configured/entered base URL
 * directly. Pair with the `rewrites()` in next.config.ts (same env var) so
 * `/api/chorus/*` is proxied server-side to `NEXT_PUBLIC_CHORUS_API_URL` —
 * a same-origin deployment then needs no `CHORUS_CORS_ORIGINS` entry at
 * all. Off by default. See web/README.md "Deploying same-origin (no
 * CORS)" and docs/REVIEW_WAVE1.md #13. */
export const CHORUS_PROXY_ENABLED = process.env.NEXT_PUBLIC_CHORUS_PROXY === "1";

/** Same-origin path next.config.ts's rewrite maps onto
 * `NEXT_PUBLIC_CHORUS_API_URL` when `CHORUS_PROXY_ENABLED` is true. */
export const CHORUS_PROXY_BASE_PATH = "/api/chorus";

/** How often /jobs/[id] and /compare poll GET /digest/{job_id} while a job
 * is in flight (SKILL.md: queued | digest_ready are non-terminal). */
export const POLL_INTERVAL_MS = 3_000;
