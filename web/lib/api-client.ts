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
  OpmlImportResult,
  Persona,
  PodcastSearchResult,
  PreviewRequest,
  PreviewResponse,
  RunResult,
  SelectionRequest,
  ShowListing,
  Source,
  Subscription,
  SubscriptionCreateRequest,
  SubscriptionUpdateRequest,
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

/** Parse a success body as JSON; an empty body (202/204) yields `undefined`
 * rather than a parse error. */
async function readJson<T>(res: Response): Promise<T> {
  const text = await res.text();
  return (text ? JSON.parse(text) : undefined) as T;
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
  return readJson<T>(res);
}

async function mockFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`/api/mock${path}`, init);
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res));
  return readJson<T>(res);
}

/** One call site for "live or mock": every subscribe-flow function below
 * goes through this, so MOCK_MODE is still a single switch. */
function request<T>(
  baseUrl: string,
  token: string,
  method: "GET" | "POST" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const init: RequestInit = { method, signal };
  if (body !== undefined) init.body = JSON.stringify(body);
  if (MOCK_MODE) return mockFetch<T>(path, init);
  return authedFetch<T>(baseUrl, token, path, init);
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

// --- Human subscribe flow ----------------------------------------------------
// Everything below needs `Authorization: Bearer <token>` except requestKey
// (POST /keys is how a person gets a token in the first place). Same-origin
// proxy mode and the mock switch come from request()/authedFetch above.

/** POST /keys {email}: 202. The email carries the key and, when the operator
 * set CHORUS_VIEWER_URL on the API, a `<viewer>/subscribe#token=` link. */
export async function requestKey(baseUrl: string, email: string): Promise<void> {
  await request<unknown>(baseUrl, "", "POST", "/keys", { email });
}

export function searchPodcasts(
  baseUrl: string,
  token: string,
  query: string,
  limit = 10,
  signal?: AbortSignal,
): Promise<PodcastSearchResult[]> {
  const qs = new URLSearchParams({ q: query, limit: String(limit) });
  return request<PodcastSearchResult[]>(
    baseUrl,
    token,
    "GET",
    `/podcasts/search?${qs.toString()}`,
    undefined,
    signal,
  );
}

/** Rejects with ApiError(422, detail) when the link is not an RSS URL, an
 * Apple Podcasts show URL, or a YouTube channel URL. */
export function resolvePodcast(baseUrl: string, token: string, url: string): Promise<Source> {
  return request<Source>(baseUrl, token, "POST", "/podcasts/resolve", { url });
}

export function importOpml(baseUrl: string, token: string, opml: string): Promise<OpmlImportResult> {
  return request<OpmlImportResult>(baseUrl, token, "POST", "/podcasts/import-opml", { opml });
}

export function previewSubscription(
  baseUrl: string,
  token: string,
  body: PreviewRequest,
): Promise<PreviewResponse> {
  return request<PreviewResponse>(baseUrl, token, "POST", "/subscriptions/preview", body);
}

export async function buildSoulFromInterview(
  baseUrl: string,
  token: string,
  answers: Record<string, string>,
): Promise<string> {
  const { soul } = await request<{ soul: string }>(baseUrl, token, "POST", "/souls/interview", {
    answers,
  });
  return soul;
}

export function createSubscription(
  baseUrl: string,
  token: string,
  body: SubscriptionCreateRequest,
): Promise<Subscription> {
  return request<Subscription>(baseUrl, token, "POST", "/subscriptions", body);
}

export function listSubscriptions(baseUrl: string, token: string): Promise<Subscription[]> {
  return request<Subscription[]>(baseUrl, token, "GET", "/subscriptions");
}

export function updateSubscription(
  baseUrl: string,
  token: string,
  subscriptionId: string,
  patch: SubscriptionUpdateRequest,
): Promise<Subscription> {
  return request<Subscription>(
    baseUrl,
    token,
    "PATCH",
    `/subscriptions/${encodeURIComponent(subscriptionId)}`,
    patch,
  );
}

export async function deleteSubscription(
  baseUrl: string,
  token: string,
  subscriptionId: string,
): Promise<void> {
  await request<unknown>(
    baseUrl,
    token,
    "DELETE",
    `/subscriptions/${encodeURIComponent(subscriptionId)}`,
  );
}

export function runSubscription(
  baseUrl: string,
  token: string,
  subscriptionId: string,
): Promise<RunResult> {
  return request<RunResult>(
    baseUrl,
    token,
    "POST",
    `/subscriptions/${encodeURIComponent(subscriptionId)}/run`,
  );
}

/** The committed sample OPML export, for trying the import without a file
 * (mock mode only; the route ships in every build but the UI only offers it
 * in mock mode). */
export async function fetchSampleOpml(): Promise<string> {
  const res = await fetch("/api/samples/opml");
  if (!res.ok) throw new ApiError(res.status, await errorDetail(res));
  return res.text();
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
