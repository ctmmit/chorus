import { NextResponse } from "next/server";

import { readMockJob } from "@/lib/server/mock-fixtures";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ jobId: string }> },
): Promise<NextResponse> {
  const { jobId } = await params;
  const job = await readMockJob(jobId);
  if (!job) {
    return NextResponse.json({ detail: "unknown job_id" }, { status: 404 });
  }
  return NextResponse.json(job);
}
