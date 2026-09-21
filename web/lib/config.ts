/** Mock mode serves web/mocks/*.json through the /api/mock route handlers
 * instead of calling a real Chorus API, so the UI runs with no backend. */
export const MOCK_MODE = process.env.NEXT_PUBLIC_CHORUS_MOCK === "1";

/** Default base URL for a real deployment; the connect form on `/` can
 * override this per-browser (persisted to localStorage). */
export const DEFAULT_API_BASE_URL = process.env.NEXT_PUBLIC_CHORUS_API_URL ?? "";

/** How often /jobs/[id] and /compare poll GET /digest/{job_id} while a job
 * is in flight (SKILL.md: queued | digest_ready are non-terminal). */
export const POLL_INTERVAL_MS = 3_000;
