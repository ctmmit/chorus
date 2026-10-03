import { NextResponse } from "next/server";

import { placeholderArtworkSvg } from "@/lib/server/mock-subscribe";

/** GET /artwork?t=<title> (mock only): a flat placeholder cover so mock
 * search results have artwork without hot-linking third-party images. */
export async function GET(request: Request): Promise<NextResponse> {
  const title = new URL(request.url).searchParams.get("t") ?? "";
  return new NextResponse(placeholderArtworkSvg(title.slice(0, 120)), {
    headers: {
      "Content-Type": "image/svg+xml; charset=utf-8",
      "Cache-Control": "public, max-age=86400",
    },
  });
}
