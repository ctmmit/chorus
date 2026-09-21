/**
 * Pure data-shaping for the network graph (/network, Phase H — DEVELOPMENT_
 * PLAN.md §6 "Infrastructure level" / §8 row H). Kept side-effect-free and
 * d3-free so it is unit-testable with Vitest; d3-force itself only runs
 * inside NetworkGraphView's useEffect (components own the simulation, this
 * module only owns the math around it).
 */
import type { NetworkEdge, NetworkGraph, NetworkNode } from "./api-types";

const MIN_RADIUS = 6;
const MAX_RADIUS = 28;

/**
 * Radius (px) for a node's `size`, scaled by square root so *area* (not
 * radius) is proportional to size — the perceptually-correct encoding for a
 * circle/square (Tufte: never let a linear radius exaggerate a linear
 * value). Clamped into [MIN_RADIUS, MAX_RADIUS] so a size-0 show (nobody
 * listens yet) still renders as a visible node and no persona/show
 * dominates the canvas regardless of how large its count gets.
 */
export function nodeRadius(size: number, maxSizeInGraph: number): number {
  if (maxSizeInGraph <= 0) return MIN_RADIUS;
  const clampedSize = Math.min(Math.max(size, 0), maxSizeInGraph);
  const t = Math.sqrt(clampedSize / maxSizeInGraph);
  return MIN_RADIUS + t * (MAX_RADIUS - MIN_RADIUS);
}

/** Largest `size` across all nodes, or 0 for an empty graph — the
 * denominator `nodeRadius` scales against so every node in one graph shares
 * one consistent scale. */
export function maxNodeSize(nodes: NetworkNode[]): number {
  return nodes.reduce((max, n) => Math.max(max, n.size), 0);
}

/**
 * Degree (edge count touching each node id) computed from the edge list
 * directly, independent of the server-reported `size` — used to sanity
 * check the API's sizing (tested below) and as a fallback if a future edge
 * kind is added without a matching size convention.
 */
export function computeDegree(graph: NetworkGraph): Record<string, number> {
  const degree: Record<string, number> = {};
  for (const node of graph.nodes) degree[node.id] = 0;
  for (const edge of graph.edges) {
    degree[edge.source] = (degree[edge.source] ?? 0) + 1;
    degree[edge.target] = (degree[edge.target] ?? 0) + 1;
  }
  return degree;
}

/** A node shape with initial simulation state (d3-force mutates x/y/vx/vy
 * on the objects it's given, so this is deliberately a plain mutable
 * object, not the readonly NetworkNode from the API). */
export interface ForceNode extends NetworkNode {
  radius: number;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
  fx?: number | null;
  fy?: number | null;
}

/** d3-force's forceLink wants `source`/`target` to start as the node's id
 * string; it replaces them with node object references in place once the
 * simulation runs. */
export interface ForceLink extends NetworkEdge {
  source: string;
  target: string;
}

/** Deterministic initial layout: nodes placed on a circle before the force
 * simulation starts, so the first rendered frame isn't every node stacked
 * at the origin. Seeded only by index — no Math.random() — so tests and
 * server-rendered markup are reproducible. */
export function toForceNodes(graph: NetworkGraph, width: number, height: number): ForceNode[] {
  const maxSize = maxNodeSize(graph.nodes);
  const cx = width / 2;
  const cy = height / 2;
  const seedRadius = Math.min(width, height) / 3;
  return graph.nodes.map((node, i) => {
    const angle = (2 * Math.PI * i) / Math.max(graph.nodes.length, 1);
    return {
      ...node,
      radius: nodeRadius(node.size, maxSize),
      x: cx + seedRadius * Math.cos(angle),
      y: cy + seedRadius * Math.sin(angle),
    };
  });
}

export function toForceLinks(graph: NetworkGraph): ForceLink[] {
  return graph.edges.map((edge) => ({ ...edge, source: edge.source, target: edge.target }));
}
