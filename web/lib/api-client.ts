/**
 * Talks to the Chorus API (SKILL.md contract) or, in mock mode, to the
 * /api/mock/* route handlers that serve web/mocks/*.json. Every call site
 * (pages/components) goes through this module so mock vs. live is a single
 * switch (MOCK_MODE), not scattered branches.
 */
import { CHORUS_PROXY_BASE_PATH, CHORUS_PROXY_ENABLED, MOCK_MODE } from "./config";
import { absoluteUrl, isSameOriginOrRelative, trimTrailingSlash } from "./url";
import type {
  DigestRequest,
  Job,
  NetworkGraph,
  Persona,
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

/** The base URL a request actually goes to: the same-origin proxy path
 * when `NEXT_PUBLIC_CHORUS_PROXY=1` (see next.config.ts's rewrite), the
 * caller-supplied/configured base URL otherwise. */
function effectiveBaseUrl(baseUrl: string): string {
  return CHORUS_PROXY_ENABLED ? CHORUS_PROXY_BASE_PATH : baseUrl;
}

async function authedFetch<T>(
  baseUrl: string,
  token: string,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const url = `${trimTrailingSlash(effectiveBaseUrl(baseUrl))}${path}`;
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

export async function fetchJob(
  baseUrl: string,
  token: string,
  jobId: string,
  signal?: AbortSignal,
): Promise<Job> {
  if (MOCK_MODE) return mockFetch<Job>(`/digest/${encodeURIComponent(jobId)}`, { signal });
  return authedFetch<Job>(baseUrl, token, `/digest/${encodeURIComponent(jobId)}`, { signal });
}

// --- Discovery (chorus/discovery.py, Phase H) -------------------------------
// GET /network and GET /personas/{id} are public (no bearer required — see
// chorus/app.py's DISCOVERY_PUBLIC_PREFIXES), but authedFetch only adds the
// Authorization header when a token is present, so passing one through here
// is harmless and keeps a single fetch path for mock vs. live.

export async function fetchNetwork(baseUrl: string, token: string): Promise<NetworkGraph> {
  if (MOCK_MODE) return mockFetch<NetworkGraph>("/network");
  return authedFetch<NetworkGraph>(baseUrl, token, "/network");
}

/** Public listing of public personas — used by the network graph to look up
 * a persona's description for the hover panel (the /network payload itself
 * carries only id/kind/label/size, not description). */
export async function listPersonas(baseUrl: string, token: string): Promise<Persona[]> {
  if (MOCK_MODE) return mockFetch<Persona[]>("/personas");
  return authedFetch<Persona[]>(baseUrl, token, "/personas");
}

export async function fetchPersona(
  baseUrl: string,
  token: string,
  personaId: string,
): Promise<Persona> {
  if (MOCK_MODE) return mockFetch<Persona>(`/personas/${encodeURIComponent(personaId)}`);
  return authedFetch<Persona>(baseUrl, token, `/personas/${encodeURIComponent(personaId)}`);
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
 *
 * The Chorus bearer token is only ever attached when `audioPath` is
 * relative (it resolves onto the configured API origin) or is an absolute
 * URL whose origin exactly matches that API origin. `Job.audio_url` can
 * currently be an absolute third-party URL (e.g. Vercel Blob) — the
 * backend is moving to always-relative artifact URLs served through an
 * owner-checked route, but until every deployment has migrated, a
 * malformed or attacker-influenced absolute URL must never receive the
 * credential (see docs/REVIEW_WAVE1.md #12).
 */
export async function fetchAudioObjectUrl(
  baseUrl: string,
  token: string,
  audioPath: string,
): Promise<string> {
  if (MOCK_MODE) {
    throw new ApiError(0, "Audio playback is not available in mock mode.");
  }
  const effective = effectiveBaseUrl(baseUrl);
  const url = absoluteUrl(effective, audioPath);
  const headers = new Headers();
  if (token && isSameOriginOrRelative(audioPath, effective)) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const res = await fetch(url, { headers });
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res));
  const blob = await res.blob();
  return URL.createObjectURL(blob);
}
