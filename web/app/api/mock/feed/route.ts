import { NextResponse } from "next/server";

import type { FeedInfo } from "@/lib/api-types";

/** GET /feed in mock mode: a placeholder private feed URL. */
export async function GET(request: Request): Promise<NextResponse> {
  const origin = new URL(request.url).origin;
  const feed: FeedInfo = {
    feed_url: `${origin}/feed/mock-feed-token.xml`,
    episodes: 2,
    instructions:
      "Add this URL to a podcast app that accepts private feeds. Treat it like a password.",
  };
  return NextResponse.json(feed);
}
