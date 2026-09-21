import { NextResponse } from "next/server";

import type { SelectionRequest } from "@/lib/api-types";
import { pickMockJobForSoul } from "@/lib/server/mock-fixtures";

export async function POST(request: Request): Promise<NextResponse> {
  const body = (await request.json()) as Partial<SelectionRequest>;
  const hasSelection = (body.shows?.length ?? 0) > 0 || (body.video_ids?.length ?? 0) > 0;
  if (!hasSelection) {
    return NextResponse.json({ detail: "selection resolved to no episodes" }, { status: 400 });
  }
  const job = await pickMockJobForSoul(body.soul ?? "");
  return NextResponse.json({ job_id: job.job_id });
}
