"use client";

import {
  forceCenter,
  forceCollide,
  forceLink,
  forceManyBody,
  forceSimulation,
} from "d3-force";
import Link from "next/link";
import { useEffect, useState } from "react";

import type { NetworkGraph } from "@/lib/api-types";
import { type ForceNode, toForceLinks, toForceNodes } from "@/lib/network";

const VIEW_WIDTH = 900;
const VIEW_HEIGHT = 560;
const CHARGE_STRENGTH = -220;
const LINK_DISTANCE = 90;
const COLLIDE_PADDING = 6;
const SIMULATION_ALPHA_DECAY = 0.04; // settles in ~a couple seconds, not instantly rigid

/** A link as d3-force mutates it in place: source/target start as the id
 * string (ForceLink) and become the resolved ForceNode object once the
 * simulation's first tick runs. */
interface RuntimeLink {
  source: string | ForceNode;
  target: string | ForceNode;
  kind: "listens_to";
}

function endpointOf(ref: string | ForceNode): ForceNode | null {
  return typeof ref === "string" ? null : ref;
}

export function NetworkGraphView({
  graph,
  descriptionsById,
}: {
  graph: NetworkGraph;
  /** persona_id -> description, for the hover panel (GET /network itself
   * carries no description — see lib/api-client.ts listPersonas). */
  descriptionsById: Record<string, string>;
}) {
  const [nodes, setNodes] = useState<ForceNode[]>([]);
  const [links, setLinks] = useState<RuntimeLink[]>([]);
  const [hoveredId, setHoveredId] = useState<string | null>(null);

  useEffect(() => {
    const simNodes = toForceNodes(graph, VIEW_WIDTH, VIEW_HEIGHT);
    const simLinks: RuntimeLink[] = toForceLinks(graph);

    const simulation = forceSimulation(simNodes)
      .alphaDecay(SIMULATION_ALPHA_DECAY)
      .force("charge", forceManyBody().strength(CHARGE_STRENGTH))
      .force(
        "link",
        forceLink<ForceNode, RuntimeLink>(simLinks)
          .id((d) => d.id)
          .distance(LINK_DISTANCE),
      )
      .force("center", forceCenter(VIEW_WIDTH / 2, VIEW_HEIGHT / 2))
      .force(
        "collide",
        forceCollide<ForceNode>((d) => d.radius + COLLIDE_PADDING),
      )
      .on("tick", () => {
        setNodes([...simNodes]);
        setLinks([...simLinks]);
      });

    return () => {
      simulation.stop();
    };
  }, [graph]);

  const hovered = nodes.find((n) => n.id === hoveredId) ?? null;
  const hoveredDescription = hovered && hovered.kind === "persona" ? descriptionsById[hovered.id] : undefined;

  return (
    <section aria-label="Persona network graph" className="space-y-3">
      <svg
        viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
        className="h-auto w-full border border-taupe"
        role="img"
        aria-label={`Network graph: ${graph.nodes.filter((n) => n.kind === "persona").length} personas, ${graph.nodes.filter((n) => n.kind === "show").length} shows`}
      >
        <g stroke="var(--taupe)" strokeWidth={1}>
          {links.map((link, i) => {
            const source = endpointOf(link.source);
            const target = endpointOf(link.target);
            if (!source || !target || source.x == null || target.x == null) return null;
            return (
              <line
                key={i}
                x1={source.x}
                y1={source.y}
                x2={target.x}
                y2={target.y}
              />
            );
          })}
        </g>
        <g>
          {nodes.map((node) => {
            if (node.x == null || node.y == null) return null;
            const isPersona = node.kind === "persona";
            const fill = isPersona ? "var(--navy)" : "var(--silver)";
            const shape = isPersona ? (
              <circle
                cx={node.x}
                cy={node.y}
                r={node.radius}
                fill={fill}
                opacity={hoveredId && hoveredId !== node.id ? 0.45 : 1}
              />
            ) : (
              <rect
                x={node.x - node.radius}
                y={node.y - node.radius}
                width={node.radius * 2}
                height={node.radius * 2}
                fill={fill}
                opacity={hoveredId && hoveredId !== node.id ? 0.45 : 1}
              />
            );
            const label = (
              <text
                x={node.x}
                y={node.y + node.radius + 12}
                textAnchor="middle"
                className="label-caps"
                fill="var(--silver)"
                fontSize={10}
              >
                {node.label}
              </text>
            );
            const content = (
              <g
                onMouseEnter={() => setHoveredId(node.id)}
                onMouseLeave={() => setHoveredId((cur) => (cur === node.id ? null : cur))}
              >
                <title>
                  {node.label} · {node.kind} · {node.size}
                  {node.kind === "persona" ? " show(s)" : " listener(s)"}
                </title>
                {shape}
                {label}
              </g>
            );
            return (
              <g key={node.id} className="cursor-pointer focus-visible:outline-blue">
                {isPersona ? (
                  <Link
                    href={`/personas/${encodeURIComponent(node.id)}`}
                    aria-label={`View persona ${node.label}`}
                  >
                    {content}
                  </Link>
                ) : (
                  content
                )}
              </g>
            );
          })}
        </g>
      </svg>

      <div className="min-h-12 border-t border-taupe pt-3">
        {hovered ? (
          <>
            <p className="label-caps">
              {hovered.label} <span className="normal-case text-ink">— {hovered.kind}</span>
            </p>
            {hoveredDescription ? (
              <p className="mt-1 font-serif text-sm italic text-silver">{hoveredDescription}</p>
            ) : null}
          </>
        ) : (
          <p className="label-caps">Hover a node for details · click a persona to open it</p>
        )}
      </div>
    </section>
  );
}
