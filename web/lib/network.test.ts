import { describe, expect, it } from "vitest";

import type { NetworkGraph } from "./api-types";
import { computeDegree, maxNodeSize, nodeRadius, toForceLinks, toForceNodes } from "./network";

function graph(): NetworkGraph {
  return {
    nodes: [
      { id: "p1", kind: "persona", label: "Value Investor", size: 2 },
      { id: "p2", kind: "persona", label: "Pop-Culture Critic", size: 0 },
      { id: "show:a", kind: "show", label: "Show A", size: 1 },
      { id: "show:b", kind: "show", label: "Show B", size: 1 },
    ],
    edges: [
      { source: "p1", target: "show:a", kind: "listens_to" },
      { source: "p1", target: "show:b", kind: "listens_to" },
    ],
  };
}

describe("maxNodeSize", () => {
  it("returns the largest size across nodes", () => {
    expect(maxNodeSize(graph().nodes)).toBe(2);
  });

  it("returns 0 for an empty node list", () => {
    expect(maxNodeSize([])).toBe(0);
  });
});

describe("nodeRadius", () => {
  it("clamps to the minimum radius when the graph has no size at all", () => {
    expect(nodeRadius(0, 0)).toBeGreaterThan(0);
  });

  it("is monotonically non-decreasing in size", () => {
    const max = 10;
    const radii = [0, 1, 5, 10].map((s) => nodeRadius(s, max));
    for (let i = 1; i < radii.length; i++) {
      expect(radii[i]).toBeGreaterThanOrEqual(radii[i - 1]);
    }
  });

  it("gives a 0-size node a visibly nonzero radius (not invisible)", () => {
    expect(nodeRadius(0, 10)).toBeGreaterThan(0);
  });

  it("caps a size beyond the graph max at the same radius as the max", () => {
    expect(nodeRadius(50, 10)).toBe(nodeRadius(10, 10));
  });
});

describe("computeDegree", () => {
  it("counts edges touching each node, including 0 for an isolated node", () => {
    const degree = computeDegree(graph());
    expect(degree.p1).toBe(2);
    expect(degree.p2).toBe(0);
    expect(degree["show:a"]).toBe(1);
    expect(degree["show:b"]).toBe(1);
  });

  it("returns an empty record for an empty graph", () => {
    expect(computeDegree({ nodes: [], edges: [] })).toEqual({});
  });
});

describe("toForceNodes", () => {
  it("produces one force node per graph node, each with a radius and seeded x/y", () => {
    const nodes = toForceNodes(graph(), 800, 600);
    expect(nodes).toHaveLength(4);
    for (const n of nodes) {
      expect(n.radius).toBeGreaterThan(0);
      expect(Number.isFinite(n.x)).toBe(true);
      expect(Number.isFinite(n.y)).toBe(true);
    }
  });

  it("is deterministic across calls (no randomness)", () => {
    const a = toForceNodes(graph(), 800, 600);
    const b = toForceNodes(graph(), 800, 600);
    expect(a.map((n) => [n.x, n.y])).toEqual(b.map((n) => [n.x, n.y]));
  });

  it("handles an empty graph without dividing by zero", () => {
    expect(toForceNodes({ nodes: [], edges: [] }, 800, 600)).toEqual([]);
  });
});

describe("toForceLinks", () => {
  it("carries source/target/kind through unchanged", () => {
    const links = toForceLinks(graph());
    expect(links).toEqual([
      { source: "p1", target: "show:a", kind: "listens_to" },
      { source: "p1", target: "show:b", kind: "listens_to" },
    ]);
  });
});
