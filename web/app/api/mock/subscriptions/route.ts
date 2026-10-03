import { NextResponse } from "next/server";

import type { SubscriptionCreateRequest } from "@/lib/api-types";
import { createMockSubscription, listMockSubscriptions } from "@/lib/server/mock-store";
import { readBody, unprocessable } from "@/lib/server/mock-http";
import { validateCreateRequest } from "@/lib/server/mock-subscribe";

/** GET /subscriptions (mock): the in-memory list for this dev-server run. */
export async function GET(): Promise<NextResponse> {
  return NextResponse.json(listMockSubscriptions());
}

/** POST /subscriptions (mock): validates like the real API (422 + detail). */
export async function POST(request: Request): Promise<NextResponse> {
  const body = await readBody<Partial<SubscriptionCreateRequest>>(request);
  if (!body) return unprocessable("request body must be JSON");
  const problem = validateCreateRequest(body);
  if (problem) return unprocessable(problem);
  return NextResponse.json(createMockSubscription(body as SubscriptionCreateRequest), { status: 201 });
}
