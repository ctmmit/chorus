import { NextResponse } from "next/server";

import type { DigestRequest } from "@/lib/api-types";
import { pickMockJobForSoul } from "@/lib/server/mock-fixtures";

export async function POST(request: Request): Promise<NextResponse> {
  const body = (await request.json()) as Partial<DigestRequest>;
  const job = await pickMockJobForSoul(body.soul ?? "");
  return NextResponse.json({ job_id: job.job_id });
}
