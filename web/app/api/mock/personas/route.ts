import { NextResponse } from "next/server";

import { readMockPersonas } from "@/lib/server/mock-fixtures";

export async function GET(): Promise<NextResponse> {
  const personas = await readMockPersonas();
  return NextResponse.json(personas);
}
