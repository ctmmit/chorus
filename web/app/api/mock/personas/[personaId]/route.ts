import { NextResponse } from "next/server";

import { readMockPersona } from "@/lib/server/mock-fixtures";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ personaId: string }> },
): Promise<NextResponse> {
  const { personaId } = await params;
  const persona = await readMockPersona(personaId);
  if (!persona) {
    return NextResponse.json({ detail: "unknown persona_id" }, { status: 404 });
  }
  return NextResponse.json(persona);
}
