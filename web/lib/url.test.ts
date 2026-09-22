import { describe, expect, it } from "vitest";

import { absoluteUrl, isSameOriginOrRelative, trimTrailingSlash } from "./url";

describe("trimTrailingSlash", () => {
  it("removes a single trailing slash", () => {
    expect(trimTrailingSlash("http://localhost:8000/")).toBe("http://localhost:8000");
  });

  it("leaves a URL with no trailing slash unchanged", () => {
    expect(trimTrailingSlash("http://localhost:8000")).toBe("http://localhost:8000");
  });
});

describe("absoluteUrl", () => {
  it("resolves a relative path against the base URL", () => {
    expect(absoluteUrl("http://localhost:8000", "/artifacts/x.mp3")).toBe(
      "http://localhost:8000/artifacts/x.mp3",
    );
  });

  it("passes an already-absolute URL through unchanged", () => {
    expect(absoluteUrl("http://localhost:8000", "https://blob.vercel-storage.com/x.mp3")).toBe(
      "https://blob.vercel-storage.com/x.mp3",
    );
  });
});

describe("isSameOriginOrRelative", () => {
  it("treats a relative path as safe — it resolves onto the API origin", () => {
    expect(isSameOriginOrRelative("/artifacts/x.mp3", "http://localhost:8000")).toBe(true);
  });

  it("treats a relative path as safe against a relative (proxied) API base", () => {
    expect(isSameOriginOrRelative("/artifacts/x.mp3", "/api/chorus")).toBe(true);
  });

  it("accepts an absolute URL on the same origin as the API base", () => {
    expect(
      isSameOriginOrRelative("https://api.example.com/artifacts/x.mp3", "https://api.example.com"),
    ).toBe(true);
  });

  it("accepts a same-origin absolute URL even with a different path/port normalization", () => {
    expect(
      isSameOriginOrRelative("http://localhost:8000/artifacts/x.mp3", "http://localhost:8000/"),
    ).toBe(true);
  });

  it("rejects a different-origin absolute URL such as a blob storage host", () => {
    expect(
      isSameOriginOrRelative(
        "https://abc123.public.blob.vercel-storage.com/x.mp3",
        "https://api.example.com",
      ),
    ).toBe(false);
  });

  it("rejects an attacker-controlled origin", () => {
    expect(isSameOriginOrRelative("https://evil.example/x.mp3", "https://api.example.com")).toBe(
      false,
    );
  });

  it("rejects a same-host URL on a different scheme", () => {
    expect(isSameOriginOrRelative("http://api.example.com/x.mp3", "https://api.example.com")).toBe(
      false,
    );
  });

  it("rejects a same-host URL on a different port", () => {
    expect(
      isSameOriginOrRelative("https://api.example.com:9000/x.mp3", "https://api.example.com"),
    ).toBe(false);
  });

  it("fails closed on a malformed absolute URL", () => {
    expect(isSameOriginOrRelative("https://", "https://api.example.com")).toBe(false);
  });

  it("fails closed when the configured API base URL itself is malformed", () => {
    expect(isSameOriginOrRelative("https://api.example.com/x.mp3", "not-a-url")).toBe(false);
  });

  it("fails closed when the configured API base URL is empty", () => {
    expect(isSameOriginOrRelative("https://api.example.com/x.mp3", "")).toBe(false);
  });
});
