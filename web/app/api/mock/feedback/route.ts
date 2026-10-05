import { NextResponse } from "next/server";

import type { RateRequest } from "@/lib/api-types";
import { readBody, unprocessable } from "@/lib/server/mock-http";

/** POST /feedback in mock mode: accepts a valid rating and echoes it. */
export async function POST(request: Request): Promise<NextResponse> {
  const body = await readBody<RateRequest>(request);
  if (!body || !body.job_id || !body.highlight_id || (body.vote !== "up" && body.vote !== "down")) {
    return unprocessable("job_id, highlight_id and vote (up or down) are required");
  }
  return NextResponse.json({ recorded: { ...body, note: body.note ?? "" } });
}
