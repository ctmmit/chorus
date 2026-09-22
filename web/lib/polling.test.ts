import { describe, expect, it } from "vitest";

import {
  BACKOFF_CAP_MS,
  BACKOFF_START_MS,
  MAX_ELAPSED_MS,
  backoffDelayMs,
  isAbortError,
  isMaxElapsedExceeded,
} from "./polling";

describe("backoffDelayMs", () => {
  it("is half of BACKOFF_START_MS on attempt 1 with zero jitter", () => {
    expect(backoffDelayMs(1, () => 0)).toBe(BACKOFF_START_MS / 2);
  });

  it("reaches BACKOFF_START_MS on attempt 1 with maximum jitter", () => {
    expect(backoffDelayMs(1, () => 0.999999)).toBeCloseTo(BACKOFF_START_MS, -1);
  });

  it("doubles the (uncapped) exponential base on successive attempts", () => {
    // rand=0 -> jitterFraction=0.5 -> delay = capped/2
    expect(backoffDelayMs(1, () => 0)).toBe(1_500); // base 3000 / 2
    expect(backoffDelayMs(2, () => 0)).toBe(3_000); // base 6000 / 2
    expect(backoffDelayMs(3, () => 0)).toBe(6_000); // base 12000 / 2
    expect(backoffDelayMs(4, () => 0)).toBe(12_000); // base 24000 / 2
  });

  it("caps the exponential growth at BACKOFF_CAP_MS before jitter is applied", () => {
    // attempt 5 -> base = 3000 * 2^4 = 48000, capped to 30000
    expect(backoffDelayMs(5, () => 0)).toBe(BACKOFF_CAP_MS / 2);
    expect(backoffDelayMs(5, () => 0.999999)).toBeCloseTo(BACKOFF_CAP_MS, -1);
  });

  it("never exceeds BACKOFF_CAP_MS however large the attempt number", () => {
    expect(backoffDelayMs(20, () => 1)).toBeLessThanOrEqual(BACKOFF_CAP_MS);
    expect(backoffDelayMs(20, () => 0)).toBeGreaterThanOrEqual(BACKOFF_CAP_MS / 2);
  });

  it("always returns at least half the capped delay (the jitter floor)", () => {
    for (const attempt of [1, 2, 3, 4, 5, 10]) {
      expect(backoffDelayMs(attempt, () => 0)).toBeGreaterThanOrEqual(BACKOFF_START_MS / 2);
    }
  });

  it("treats attempt 0 or negative as attempt 1", () => {
    expect(backoffDelayMs(0, () => 0)).toBe(backoffDelayMs(1, () => 0));
    expect(backoffDelayMs(-3, () => 0)).toBe(backoffDelayMs(1, () => 0));
  });

  it("defaults to Math.random when no random function is supplied, staying in range", () => {
    const delay = backoffDelayMs(1);
    expect(delay).toBeGreaterThanOrEqual(BACKOFF_START_MS / 2);
    expect(delay).toBeLessThanOrEqual(BACKOFF_START_MS);
  });
});

describe("isMaxElapsedExceeded", () => {
  const start = 1_000_000;

  it("is false before the ceiling is reached", () => {
    expect(isMaxElapsedExceeded(start, start + MAX_ELAPSED_MS - 1)).toBe(false);
  });

  it("is true exactly at the ceiling", () => {
    expect(isMaxElapsedExceeded(start, start + MAX_ELAPSED_MS)).toBe(true);
  });

  it("is true well past the ceiling", () => {
    expect(isMaxElapsedExceeded(start, start + MAX_ELAPSED_MS * 2)).toBe(true);
  });

  it("is false at the start", () => {
    expect(isMaxElapsedExceeded(start, start)).toBe(false);
  });
});

describe("isAbortError", () => {
  it("recognizes a DOMException-shaped AbortError", () => {
    expect(isAbortError(new DOMException("aborted", "AbortError"))).toBe(true);
  });

  it("recognizes a plain object with name AbortError", () => {
    expect(isAbortError({ name: "AbortError", message: "The operation was aborted." })).toBe(true);
  });

  it("rejects a regular Error", () => {
    expect(isAbortError(new Error("network down"))).toBe(false);
  });

  it("rejects non-error values", () => {
    expect(isAbortError(null)).toBe(false);
    expect(isAbortError(undefined)).toBe(false);
    expect(isAbortError("AbortError")).toBe(false);
    expect(isAbortError(42)).toBe(false);
  });
});
