import { NextResponse } from "next/server";

import { readBody, unprocessable } from "@/lib/server/mock-http";
import { renderSoulFromInterview } from "@/lib/server/mock-subscribe";
import { interviewHasAnswers } from "@/lib/subscribe";

/** POST /souls/interview {answers} (mock): the same six-section soul the
 * real MockSoulBuilder renders (chorus/bootstrap.py). */
export async function POST(request: Request): Promise<NextResponse> {
  const body = await readBody<{ answers?: unknown }>(request);
  const answers = body?.answers;
  if (typeof answers !== "object" || answers === null || Array.isArray(answers)) {
    return unprocessable("answers must be an object");
  }
  const record = answers as Record<string, string>;
  if (!interviewHasAnswers(record)) return unprocessable("answer at least one question");
  return NextResponse.json({ soul: renderSoulFromInterview(record) });
}
