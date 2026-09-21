/**
 * Talks to the Chorus API (SKILL.md contract) or, in mock mode, to the
 * /api/mock/* route handlers that serve web/mocks/*.json. Every call site
 * (pages/components) goes through this module so mock vs. live is a single
 * switch (MOCK_MODE), not scattered branches.
 */
import { MOCK_MODE } from "./config";
import { absoluteUrl, trimTrailingSlash } from "./url";
import type {
  DigestRequest,
  Job,
  SelectionRequest,
  ShowListing,
} from "./api-types";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function errorDetail(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
  } catch {
    // Body wasn't JSON — fall through to the status text.
  }
  return res.statusText || `request failed with status ${res.status}`;
}

async function authedFetch<T>(
  baseUrl: string,
  token: string,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const url = `${trimTrailingSlash(baseUrl)}${path}`;
  const headers = new Headers(init.headers);
  if (token) headers.set("Authorization", `Bearer ${token}`);
  if (init.body != null && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const res = await fetch(url, { ...init, headers });
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res));
  return (await res.json()) as T;
}

async function mockFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api/mock${path}`, init);
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res));
  return (await res.json()) as T;
}

export async function listShows(baseUrl: string, token: string): Promise<ShowListing[]> {
  if (MOCK_MODE) return mockFetch<ShowListing[]>("/shows");
  return authedFetch<ShowListing[]>(baseUrl, token, "/shows");
}

export async function submitDigest(
  baseUrl: string,
  token: string,
  body: DigestRequest,
): Promise<{ job_id: string }> {
  const init: RequestInit = { method: "POST", body: JSON.stringify(body) };
  if (MOCK_MODE) return mockFetch<{ job_id: string }>("/digest", init);
  return authedFetch<{ job_id: string }>(baseUrl, token, "/digest", init);
}

export async function submitDigestSelect(
  baseUrl: string,
  token: string,
  body: SelectionRequest,
): Promise<{ job_id: string }> {
  const init: RequestInit = { method: "POST", body: JSON.stringify(body) };
  if (MOCK_MODE) return mockFetch<{ job_id: string }>("/digest/select", init);
  return authedFetch<{ job_id: string }>(baseUrl, token, "/digest/select", init);
}

export async function fetchJob(baseUrl: string, token: string, jobId: string): Promise<Job> {
  if (MOCK_MODE) return mockFetch<Job>(`/digest/${encodeURIComponent(jobId)}`);
  return authedFetch<Job>(baseUrl, token, `/digest/${encodeURIComponent(jobId)}`);
}

export async function fetchSampleSoul(name: "investor" | "popculture"): Promise<string> {
  const res = await fetch(`/api/samples/souls/${name}`);
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res));
  return res.text();
}

/**
 * `<audio src>` cannot carry an Authorization header, so we fetch the
 * artifact as a blob with the bearer token and hand back an object URL.
 * Not available in mock mode — there is no artifact server behind
 * `/api/mock`, only canned JSON (see web/README.md "API gaps").
 */
export async function fetchAudioObjectUrl(
  baseUrl: string,
  token: string,
  audioPath: string,
): Promise<string> {
  if (MOCK_MODE) {
    throw new ApiError(0, "Audio playback is not available in mock mode.");
  }
  const url = absoluteUrl(baseUrl, audioPath);
  const headers = new Headers();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(url, { headers });
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res));
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}
