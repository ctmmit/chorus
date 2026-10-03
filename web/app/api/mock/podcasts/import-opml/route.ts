import { NextResponse } from "next/server";

import { readBody, unprocessable } from "@/lib/server/mock-http";
import { looksLikeOpml, parseOpml } from "@/lib/server/mock-subscribe";

/** POST /podcasts/import-opml {opml} (mock): reads feed outlines out of an
 * OPML export and reports the entries it could not use. */
export async function POST(request: Request): Promise<NextResponse> {
  const body = await readBody<{ opml?: unknown }>(request);
  if (!body || typeof body.opml !== "string") return unprocessable("opml is required");
  if (!looksLikeOpml(body.opml)) {
    return unprocessable("That file does not look like an OPML export of podcast subscriptions.");
  }
  return NextResponse.json(parseOpml(body.opml));
}
