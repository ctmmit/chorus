import { NextResponse } from "next/server";

import { readMockNetwork } from "@/lib/server/mock-fixtures";

export async function GET(): Promise<NextResponse> {
  const network = await readMockNetwork();
  return NextResponse.json(network);
}
