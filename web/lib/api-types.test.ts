import { existsSync, readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import { HOST_PERSONA_IS_SOUL, INTERVIEW_KEYS, TWO_HOST_PROFILE } from "./api-types";

const MODELS_PY = path.join(process.cwd(), "..", "chorus", "models.py");
const BOOTSTRAP_PY = path.join(process.cwd(), "..", "chorus", "bootstrap.py");

describe("TWO_HOST_PROFILE", () => {
  it("is a valid dialogue profile: exactly one host and one cohost", () => {
    expect(TWO_HOST_PROFILE.format).toBe("dialogue");
    expect(TWO_HOST_PROFILE.speakers.map((s) => s.role).sort()).toEqual(["cohost", "host"]);
    expect(TWO_HOST_PROFILE.speakers.find((s) => s.role === "host")?.persona).toBe(HOST_PERSONA_IS_SOUL);
    // Unset, like chorus/models.py: the length is budgeted from the sources.
    expect(TWO_HOST_PROFILE.style.target_minutes).toBeUndefined();
  });

  // Drift guard: runs from a full checkout (the Python source is one level
  // up) and is skipped when web/ is built on its own.
  it.skipIf(!existsSync(MODELS_PY))("matches chorus/models.py TWO_HOST_PROFILE", () => {
    const source = readFileSync(MODELS_PY, "utf-8").replace(/\r\n/g, "\n");
    const start = source.indexOf("TWO_HOST_PROFILE = EpisodeProfile(");
    expect(start).toBeGreaterThan(-1);
    const block = source.slice(start, source.indexOf("\n)\n", start));
    // Python implicit string concatenation: join the adjacent literals.
    const flat = block.replace(/"\s*\n\s*"/g, "");
    expect(flat).toContain(`name="${TWO_HOST_PROFILE.name}"`);
    expect(flat).toContain(`format="${TWO_HOST_PROFILE.format}"`);
    expect(flat).toContain(`tone="${TWO_HOST_PROFILE.style.tone}"`);
    for (const technique of TWO_HOST_PROFILE.style.engagement) {
      expect(flat).toContain(`"${technique}"`);
    }
    for (const speaker of TWO_HOST_PROFILE.speakers) {
      expect(flat).toContain(`role="${speaker.role}"`);
      expect(flat).toContain(`name="${speaker.name}"`);
    }
    const cohost = TWO_HOST_PROFILE.speakers.find((s) => s.role === "cohost");
    expect(flat).toContain(cohost?.persona ?? "<missing>");
  });
});

describe("interview keys", () => {
  it.skipIf(!existsSync(BOOTSTRAP_PY))("are the keys chorus/bootstrap.py build_from_interview reads", () => {
    const source = readFileSync(BOOTSTRAP_PY, "utf-8").replace(/\r\n/g, "\n");
    const start = source.indexOf("def build_from_interview(self, answers: dict[str, str]) -> str:\n        def split");
    expect(start).toBeGreaterThan(-1);
    const body = source.slice(start, source.indexOf("class AnthropicSoulBuilder", start));
    const read = new Set([...body.matchAll(/(?:split|answers\.get)\(\s*"(\w+)"/g)].map((m) => m[1]));
    expect([...read].sort()).toEqual([...INTERVIEW_KEYS].sort());
  });
});
