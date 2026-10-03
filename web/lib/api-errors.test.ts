import { describe, expect, it } from "vitest";

import { ApiError } from "./api-client";
import {
  AUTH_REJECTED_MESSAGE,
  NETWORK_ERROR_MESSAGE,
  describeApiError,
  isAbortError,
} from "./api-errors";

describe("describeApiError", () => {
  it("passes through an API detail", () => {
    expect(describeApiError(new ApiError(422, "That is not a feed."), "fallback")).toEqual({
      message: "That is not a feed.",
      auth: false,
    });
  });

  it("flags rejected keys", () => {
    for (const status of [401, 403]) {
      expect(describeApiError(new ApiError(status, "nope"), "fallback")).toEqual({
        message: AUTH_REJECTED_MESSAGE,
        auth: true,
      });
    }
  });

  it("uses the fallback when the API gave no message", () => {
    expect(describeApiError(new ApiError(500, ""), "Try again.").message).toBe("Try again.");
  });

  it("treats a TypeError as a network failure", () => {
    expect(describeApiError(new TypeError("Failed to fetch"), "fallback")).toEqual({
      message: NETWORK_ERROR_MESSAGE,
      auth: false,
    });
  });

  it("falls back for anything else", () => {
    expect(describeApiError("boom", "fallback")).toEqual({ message: "fallback", auth: false });
  });
});

describe("isAbortError", () => {
  it("recognizes a DOMException AbortError only", () => {
    expect(isAbortError(new DOMException("aborted", "AbortError"))).toBe(true);
    expect(isAbortError(new DOMException("x", "NetworkError"))).toBe(false);
    expect(isAbortError(new Error("AbortError"))).toBe(false);
  });
});
