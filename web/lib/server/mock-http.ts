/** Tiny helpers shared by the mock-mode route handlers. Server-only. */
import { NextResponse } from "next/server";

/** Parse a JSON request body, or null when it is missing or malformed. */
export async function readBody<T>(request: Request): Promise<T | null> {
  try {
    const parsed = (await request.json()) as unknown;
    return typeof parsed === "object" && parsed !== null ? (parsed as T) : null;
  } catch {
    return null;
  }
}

/** The API's validation-failure shape: 422 with `{detail: string}`. */
export function unprocessable(detail: string): NextResponse {
  return NextResponse.json({ detail }, { status: 422 });
}

export function notFound(detail: string): NextResponse {
  return NextResponse.json({ detail }, { status: 404 });
}
