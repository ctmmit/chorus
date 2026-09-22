"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, fetchJob } from "./api-client";
import { POLL_INTERVAL_MS } from "./config";
import { backoffDelayMs, isAbortError, isMaxElapsedExceeded } from "./polling";
import type { Job } from "./api-types";

export interface JobPollState {
  job: Job | null;
  error: string | null;
  /** True until the first response (success or error) arrives. */
  loading: boolean;
  /** True once polling has stopped because it ran for MAX_ELAPSED_MS
   * without reaching a terminal status. Call `retry()` to resume. */
  paused: boolean;
  /** Resumes polling after a pause: resets the elapsed-time ceiling and
   * error backoff, then polls immediately. No-op while not paused. */
  retry: () => void;
}

const TERMINAL_STATUSES = new Set(["done", "failed"]);

/** Polls GET /digest/{jobId}, per SKILL.md's async lifecycle, until the job
 * reaches a terminal status (done|failed). Honest about the in-between
 * states rather than only rendering the end result.
 *
 * Bounded polling (docs/REVIEW_WAVE1.md #21): errors back off exponentially
 * with jitter (starting at 3s, capped at 30s); the whole cycle stops after
 * MAX_ELAPSED_MS and surfaces `paused` instead of continuing forever; every
 * request carries its own AbortController, aborted on unmount and whenever
 * baseUrl/token/jobId change; no state is set after that happens. */
export function useJob(baseUrl: string, token: string, jobId: string): JobPollState {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(jobId !== "");
  const [paused, setPaused] = useState(false);

  // Bumped past every closure from a previous effect run (dep change or
  // unmount) so their in-flight `.then`/`.catch` continuations can tell
  // they've been superseded even after the shared ref is reused by a later
  // run — an increment-only counter can't be raced the way a boolean flag
  // reset back to "not stopped" could be.
  const epochRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);
  const timerRef = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const startedAtRef = useRef(0);
  const errorCountRef = useRef(0);
  const retryRef = useRef<() => void>(() => {});

  useEffect(() => {
    const myEpoch = ++epochRef.current;
    // Reset state synchronously before starting a new poll cycle for a
    // (possibly different) jobId — the async work that follows updates
    // state from its own callbacks, which is the pattern this lint rule
    // wants; this reset is the exception every polling hook needs.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setJob(null);
    setError(null);
    setPaused(false);

    // No job id to poll (e.g. /compare before both ids are chosen) — stay
    // idle rather than requesting GET /digest/ against an empty id.
    if (jobId === "") {
      setLoading(false);
      return;
    }
    setLoading(true);
    startedAtRef.current = Date.now();
    errorCountRef.current = 0;

    function stopped(): boolean {
      return epochRef.current !== myEpoch;
    }

    function scheduleNext(delayMs: number): void {
      if (isMaxElapsedExceeded(startedAtRef.current, Date.now())) {
        setPaused(true);
        return;
      }
      timerRef.current = setTimeout(() => void poll(), delayMs);
    }

    async function poll(): Promise<void> {
      if (stopped()) return;
      const controller = new AbortController();
      abortRef.current = controller;
      try {
        const result = await fetchJob(baseUrl, token, jobId, controller.signal);
        if (stopped()) return;
        errorCountRef.current = 0;
        setJob(result);
        setError(null);
        setLoading(false);
        if (!TERMINAL_STATUSES.has(result.status)) {
          scheduleNext(POLL_INTERVAL_MS);
        }
      } catch (err) {
        if (stopped() || isAbortError(err)) return;
        setError(err instanceof ApiError ? err.message : "Could not reach the Chorus API.");
        setLoading(false);
        // 404 (unknown job_id) and 401 (bad token) are permanent — retrying
        // won't change the outcome. Anything else (network blip, 5xx) is
        // worth another attempt, backing off so persistent failures don't
        // hammer the API.
        const permanent = err instanceof ApiError && (err.status === 404 || err.status === 401);
        if (permanent) return;
        errorCountRef.current += 1;
        scheduleNext(backoffDelayMs(errorCountRef.current));
      }
    }

    retryRef.current = () => {
      if (stopped()) return;
      if (timerRef.current) clearTimeout(timerRef.current);
      startedAtRef.current = Date.now();
      errorCountRef.current = 0;
      setPaused(false);
      setError(null);
      void poll();
    };

    void poll();

    return () => {
      epochRef.current += 1;
      if (timerRef.current) clearTimeout(timerRef.current);
      abortRef.current?.abort();
    };
  }, [baseUrl, token, jobId]);

  const retry = useCallback(() => retryRef.current(), []);

  return { job, error, loading, paused, retry };
}
