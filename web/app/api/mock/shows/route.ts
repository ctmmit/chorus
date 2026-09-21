import { NextResponse } from "next/server";

import { readMockShows } from "@/lib/server/mock-fixtures";

export async function GET(): Promise<NextResponse> {
  const shows = await readMockShows();
  return NextResponse.json(shows);
}
