import { describe, expect, it } from "vitest";

import { parseVideoIds } from "./parse";

describe("parseVideoIds", () => {
  it("splits on newlines and commas, trims, and dedups", () => {
    expect(parseVideoIds("abc\n def,ghi\n\nabc, def")).toEqual(["abc", "def", "ghi"]);
  });

  it("returns an empty array for blank input", () => {
    expect(parseVideoIds("   \n  ")).toEqual([]);
  });
});
