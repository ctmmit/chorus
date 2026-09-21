import { NextResponse } from "next/server";

import { readSampleSoul } from "@/lib/server/mock-fixtures";

/** Serves the two sample souls (fixtures/souls/*.md, copied to
 * web/mocks/souls/) for the "load sample" button on `/`. Available in both
 * mock and live mode — it's static bundled content, not a call to Chorus. */
export async function GET(
  _request: Request,
  { params }: { params: Promise<{ name: string }> },
): Promise<NextResponse> {
  const { name } = await params;
  const text = await readSampleSoul(name);
  if (text == null) {
    return NextResponse.json({ detail: "unknown sample" }, { status: 404 });
  }
  return new NextResponse(text, {
    headers: { "Content-Type": "text/markdown; charset=utf-8" },
  });
}
