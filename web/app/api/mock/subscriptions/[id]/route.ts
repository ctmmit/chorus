import { NextResponse } from "next/server";

import type { SubscriptionUpdateRequest } from "@/lib/api-types";
import { deleteMockSubscription, updateMockSubscription } from "@/lib/server/mock-store";
import { notFound, readBody, unprocessable } from "@/lib/server/mock-http";
import { isSource } from "@/lib/subscribe";

type Params = { params: Promise<{ id: string }> };

/** PATCH /subscriptions/{id} (mock): context, active, cadence, sources,
 * highlight_count, max_episodes_per_run, notify_when_empty. */
export async function PATCH(request: Request, { params }: Params): Promise<NextResponse> {
  const { id } = await params;
  const body = await readBody<SubscriptionUpdateRequest>(request);
  if (!body) return unprocessable("request body must be JSON");
  if (body.sources !== undefined) {
    if (!Array.isArray(body.sources) || body.sources.length === 0 || !body.sources.every(isSource)) {
      return unprocessable("sources must be a non-empty list of sources");
    }
  }
  if (body.cadence !== undefined && body.cadence !== "weekly" && body.cadence !== "daily") {
    return unprocessable('cadence must be "weekly" or "daily"');
  }
  const updated = updateMockSubscription(id, body);
  if (!updated) return notFound("unknown subscription_id");
  return NextResponse.json(updated);
}

/** DELETE /subscriptions/{id} (mock): 204, no body. */
export async function DELETE(_request: Request, { params }: Params): Promise<NextResponse> {
  const { id } = await params;
  if (!deleteMockSubscription(id)) return notFound("unknown subscription_id");
  return new NextResponse(null, { status: 204 });
}
