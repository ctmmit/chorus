import { NextResponse } from "next/server";

import { pickMockJobForSoul } from "@/lib/server/mock-fixtures";
import { getMockSubscription, recordMockRun } from "@/lib/server/mock-store";
import { notFound } from "@/lib/server/mock-http";

/** POST /subscriptions/{id}/run (mock): "runs" the digest by pointing at a
 * canned fixture job, so /jobs/{job_id} renders a real digest. */
export async function POST(
  _request: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<NextResponse> {
  const { id } = await params;
  const sub = getMockSubscription(id);
  if (!sub) return notFound("unknown subscription_id");
  const job = await pickMockJobForSoul(sub.soul);
  const sourceCount = sub.sources?.length ?? 0;
  recordMockRun(id, job.job_id, Math.min(sub.max_episodes_per_run, Math.max(1, sourceCount)));
  return NextResponse.json({ job_id: job.job_id });
}
