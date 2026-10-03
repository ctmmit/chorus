import { NextResponse } from "next/server";

import type { RunResult } from "@/lib/api-types";
import { pickMockJobForSoul } from "@/lib/server/mock-fixtures";
import { getMockSubscription, recordMockRun, recordMockSkippedRun } from "@/lib/server/mock-store";
import { notFound } from "@/lib/server/mock-http";
import { planRun } from "@/lib/server/mock-subscribe";

/** POST /subscriptions/{id}/run (mock). Like the real API it answers
 * `{job_id, skipped_reason}`: with new episodes it "runs" by pointing at a
 * canned fixture job, so /jobs/{job_id} renders a real digest; with none
 * (a second run within a minute, or every source unreadable) `job_id` is
 * null and `skipped_reason` says why. */
export async function POST(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<NextResponse> {
  const { id } = await params;
  const sub = getMockSubscription(id);
  if (!sub) return notFound("unknown subscription_id");

  const plan = planRun(sub, new Date());
  if (plan.kind === "skipped") {
    recordMockSkippedRun(id, plan.reason);
    const result: RunResult = { job_id: null, skipped_reason: plan.reason };
    return NextResponse.json(result);
  }
  const job = await pickMockJobForSoul(sub.soul);
  recordMockRun(id, job.job_id, plan.newEpisodes);
  const result: RunResult = { job_id: job.job_id, skipped_reason: null };
  return NextResponse.json(result);
}
