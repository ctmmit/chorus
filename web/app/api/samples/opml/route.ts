import { NextResponse } from "next/server";

import { readSampleOpml } from "@/lib/server/mock-fixtures";

/** Serves the committed sample OPML export for the "Use the sample file"
 * button in mock mode. Static bundled content, not a call to Chorus. */
export async function GET(): Promise<NextResponse> {
  return new NextResponse(await readSampleOpml(), {
    headers: { "Content-Type": "text/x-opml; charset=utf-8" },
  });
}
