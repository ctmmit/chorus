import { NextResponse } from "next/server";

import { readMockPodcastCatalog } from "@/lib/server/mock-fixtures";
import { readBody, unprocessable } from "@/lib/server/mock-http";
import { resolveLink } from "@/lib/server/mock-subscribe";

/** POST /podcasts/resolve {url} (mock): recognizes an RSS URL, an Apple
 * Podcasts show URL, or a YouTube channel URL; anything else is a 422. */
export async function POST(request: Request): Promise<NextResponse> {
  const body = await readBody<{ url?: unknown }>(request);
  if (!body || typeof body.url !== "string") return unprocessable("url is required");
  const outcome = resolveLink(body.url, await readMockPodcastCatalog());
  if (!outcome.ok) return unprocessable(outcome.detail);
  return NextResponse.json(outcome.source);
}
