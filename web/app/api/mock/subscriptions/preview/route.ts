import { NextResponse } from "next/server";

import type { PreviewRequest } from "@/lib/api-types";
import { readBody, unprocessable } from "@/lib/server/mock-http";
import { buildMockPreview } from "@/lib/server/mock-subscribe";
import { isSource } from "@/lib/subscribe";

/** POST /subscriptions/preview (mock): a plausible week of episodes for the
 * chosen sources. */
export async function POST(request: Request): Promise<NextResponse> {
  const body = await readBody<Partial<PreviewRequest>>(request);
  if (!body || !Array.isArray(body.sources) || !body.sources.every(isSource)) {
    return unprocessable("sources must be a list of sources");
  }
  if (body.sources.length === 0) return unprocessable("add at least one source");
  const lookback = Number(body.lookback_days);
  const max = Number(body.max_episodes_per_run);
  if (!Number.isFinite(lookback) || lookback < 1) return unprocessable("lookback_days must be at least 1");
  if (!Number.isFinite(max) || max < 1) return unprocessable("max_episodes_per_run must be at least 1");
  return NextResponse.json(buildMockPreview(body.sources, lookback, max, new Date()));
}
