/** localStorage access, wrapped in try/catch per CLAUDE.md environment rules
 * (private browsing / disabled storage must degrade, never throw). */
import { DEFAULT_API_BASE_URL } from "./config";

const KEY_BASE_URL = "chorus.baseUrl";
const KEY_TOKEN = "chorus.token";
const KEY_RECENT_JOBS = "chorus.recentJobs";
const MAX_RECENT_JOBS = 25;

export interface RecentJob {
  job_id: string;
  base_url: string;
  created_at: string; // ISO 8601
  label: string;
  /** Video ids the viewer asked for when it submitted this job, used to
   * detect episodes that were skipped server-side (present in the request,
   * absent from the digest) — the API itself does not report skips. */
  requested_episode_ids: string[];
}

function readLocalStorage(key: string): string | null {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeLocalStorage(key: string, value: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // Storage unavailable (private browsing, quota, disabled) — degrade silently.
  }
}

export function getBaseUrl(): string {
  return readLocalStorage(KEY_BASE_URL) ?? DEFAULT_API_BASE_URL;
}

export function setBaseUrl(url: string): void {
  writeLocalStorage(KEY_BASE_URL, url);
}

export function getToken(): string {
  return readLocalStorage(KEY_TOKEN) ?? "";
}

export function setToken(token: string): void {
  writeLocalStorage(KEY_TOKEN, token);
}

export function getRecentJobs(): RecentJob[] {
  const raw = readLocalStorage(KEY_RECENT_JOBS);
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (!Array.isArray(parsed)) return [];
    return parsed as RecentJob[];
  } catch {
    return [];
  }
}

export function getRecentJob(jobId: string): RecentJob | undefined {
  return getRecentJobs().find((j) => j.job_id === jobId);
}

export function addRecentJob(entry: RecentJob): void {
  const existing = getRecentJobs().filter((j) => j.job_id !== entry.job_id);
  const next = [entry, ...existing].slice(0, MAX_RECENT_JOBS);
  writeLocalStorage(KEY_RECENT_JOBS, JSON.stringify(next));
}

export function clearRecentJobs(): void {
  writeLocalStorage(KEY_RECENT_JOBS, JSON.stringify([]));
}
