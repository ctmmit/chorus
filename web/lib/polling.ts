/**
 * Pure helpers for bounded polling (useJob.ts). Kept side-effect-free and
 * separate from the hook so the backoff schedule and elapsed-time ceiling
 * are unit-testable without React or fake timers (see docs/REVIEW_WAVE1.md
 * #21).
 */

/** First retry delay after an error, before jitter. */
export const BACKOFF_START_MS = 3_000;

/** Upper bound on any single retry delay, before jitter. */
export const BACKOFF_CAP_MS = 30_000;

/** Total wall-clock time a poll cycle may run before it pauses and asks the
 * caller to retry explicitly. */
export const MAX_ELAPSED_MS = 10 * 60 * 1000;

/**
 * Exponential backoff with jitter for the `attempt`-th consecutive error
 * (1-indexed: the delay before the *first* retry uses `attempt === 1`).
 * Doubles from `BACKOFF_START_MS`, capped at `BACKOFF_CAP_MS`, then
 * randomizes to somewhere in the top half of that capped value (50%-100%)
 * so many clients backing off together don't retry in lockstep.
 *
 * `random` is injectable for deterministic tests; it must return a value in
 * `[0, 1)`, matching `Math.random`'s contract.
 */
export function backoffDelayMs(attempt: number, random: () => number = Math.random): number {
  const n = Math.max(1, Math.floor(attempt));
  const exponential = BACKOFF_START_MS * 2 ** (n - 1);
  const capped = Math.min(exponential, BACKOFF_CAP_MS);
  const jitterFraction = 0.5 + random() * 0.5;
  return Math.round(capped * jitterFraction);
}

/** True once `nowMs - startedAtMs` has reached the polling ceiling. */
export function isMaxElapsedExceeded(startedAtMs: number, nowMs: number): boolean {
  return nowMs - startedAtMs >= MAX_ELAPSED_MS;
}

/** Duck-types the DOMException/Error thrown when an in-flight `fetch` is
 * cancelled via `AbortController.abort()`. Not every environment's
 * AbortError is an `instanceof Error` (DOMException isn't one in all
 * runtimes), so this checks `.name` instead of relying on the prototype
 * chain. */
export function isAbortError(err: unknown): boolean {
  return (
    typeof err === "object" &&
    err !== null &&
    "name" in err &&
    (err as { name?: unknown }).name === "AbortError"
  );
}
