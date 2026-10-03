import { NextResponse } from "next/server";

import { isValidEmail } from "@/lib/subscribe";
import { readBody, unprocessable } from "@/lib/server/mock-http";

/** POST /keys (mock). The real API emails a key (and, when the operator set
 * CHORUS_VIEWER_URL, a `<viewer>/subscribe#token=` link). Nothing is sent
 * here; the wizard tells the user to paste any text as the key. */
export async function POST(request: Request): Promise<NextResponse> {
  const body = await readBody<{ email?: unknown }>(request);
  if (!body || typeof body.email !== "string" || !isValidEmail(body.email)) {
    return unprocessable("email must be a valid address");
  }
  return NextResponse.json({ detail: "key sent" }, { status: 202 });
}
