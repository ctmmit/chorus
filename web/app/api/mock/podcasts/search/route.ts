import { NextResponse } from "next/server";

import { readMockPodcastCatalog } from "@/lib/server/mock-fixtures";
import { DEFAULT_SEARCH_LIMIT, searchCatalog } from "@/lib/server/mock-subscribe";

/** GET /podcasts/search?q=&limit= (mock): matches the committed catalog. */
export async function GET(request: Request): Promise<NextResponse> {
  const params = new URL(request.url).searchParams;
  const q = params.get("q") ?? "";
  const limit = Number(params.get("limit")) || DEFAULT_SEARCH_LIMIT;
  const catalog = await readMockPodcastCatalog();
  return NextResponse.json(searchCatalog(catalog, q, limit));
}
