"use client";

import { useEffect, useRef, useState } from "react";

import { ApiError, fetchJob } from "./api-client";
import { POLL_INTERVAL_MS } from "./config";
import type { Job } from "./api-types";

export interface JobPollState {
  job: Job | null;
  error: string | null;
  /** True until the first response (success or error) arrives. */
  loading: boolean;
}

const TERMINAL_STATUSES = new Set(["done", "failed"]);

/** Polls GET /digest/{jobId} every POLL_INTERVAL_MS until the job reaches a
 * terminal status (done|failed), per SKILL.md's async lifecycle. Honest
 * about the in-between states rather than only rendering the end result. */
export function useJob(baseUrl: string, token: string, jobId: string): JobPollState {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(jobId !== "");
  const stoppedRef = useRef(false);

  useEffect(() => {
    stoppedRef.current = false;
    // Reset state synchronously before starting a new poll cycle for a
    // (possibly different) jobId — the async work that follows updates
    // state from its own callbacks, which is the pattern this lint rule
    // wants; this reset is the exception every polling hook needs.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setJob(null);
    setError(null);

    // No job id to poll (e.g. /compare before both ids are chosen) — stay
    // idle rather than requesting GET /digest/ against an empty id.
    if (jobId === "") {
      setLoading(false);
      return;
    }
    setLoading(true);

    let timer: ReturnType<typeof setTimeout> | undefined;

    async function poll(): Promise<void> {
      if (stoppedRef.current) return;
      try {
        const result = await fetchJob(baseUrl, token, jobId);
        if (stoppedRef.current) return;
        setJob(result);
        setError(null);
        setLoading(false);
        if (!TERMINAL_STATUSES.has(result.status)) {
          timer = setTimeout(poll, POLL_INTERVAL_MS);
        }
      } catch (err) {
        if (stoppedRef.current) return;
        setError(err instanceof ApiError ? err.message : "Could not reach the Chorus API.");
        setLoading(false);
        // 404 (unknown job_id) and 401 (bad token) are permanent — retrying
        // won't change the outcome. Anything else (network blip, 5xx) is
        // worth another attempt.
        const permanent = err instanceof ApiError && (err.status === 404 || err.status === 401);
        if (!permanent) {
          timer = setTimeout(poll, POLL_INTERVAL_MS);
        }
      }
    }

    void poll();

    return () => {
      stoppedRef.current = true;
      if (timer) clearTimeout(timer);
    };
  }, [baseUrl, token, jobId]);

  return { job, error, loading };
}
