import { NextResponse } from "next/server";

import { mockAnswer } from "@/lib/server/mock-ask";
import { readMockJob } from "@/lib/server/mock-fixtures";
import { notFound, readBody, unprocessable } from "@/lib/server/mock-http";

/** POST /digest/{job_id}/ask in mock mode (see lib/server/mock-ask.ts). */
export async function POST(
  request: Request,
  { params }: { params: Promise<{ jobId: string }> },
): Promise<NextResponse> {
  const { jobId } = await params;
  const job = await readMockJob(jobId);
  if (!job) return notFound("unknown job, or no digest yet");
  const body = await readBody<{ question?: string }>(request);
  const question = body?.question?.trim() ?? "";
  if (!question) return unprocessable("question is required");
  return NextResponse.json(mockAnswer(job, question));
}
