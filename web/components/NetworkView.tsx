"use client";

import { useEffect, useState } from "react";

import { NetworkGraphView } from "@/components/NetworkGraphView";
import { ApiError, fetchNetwork, listPersonas } from "@/lib/api-client";
import type { NetworkGraph } from "@/lib/api-types";
import { getBaseUrl, getToken } from "@/lib/storage";

/** /network — the Phase H "infrastructure level" surface (DEVELOPMENT_PLAN.md
 * §6): a force-directed graph of registered personas and the shows they
 * listen to, built from GET /network (+ GET /personas for descriptions). */
export function NetworkView() {
  const [graph, setGraph] = useState<NetworkGraph | null>(null);
  const [descriptionsById, setDescriptionsById] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const baseUrl = getBaseUrl();
    const token = getToken();

    async function load(): Promise<void> {
      try {
        const [networkResult, personasResult] = await Promise.all([
          fetchNetwork(baseUrl, token),
          listPersonas(baseUrl, token),
        ]);
        if (cancelled) return;
        setGraph(networkResult);
        setDescriptionsById(
          Object.fromEntries(personasResult.map((p) => [p.persona_id, p.description])),
        );
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof ApiError ? err.message : "Could not reach the Chorus API.");
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) {
    return <p className="label-caps">Loading…</p>;
  }

  if (error && !graph) {
    return (
      <div className="space-y-2">
        <p className="font-serif text-lg text-ink">Could not reach the Chorus API.</p>
        <p className="font-mono text-sm text-red">{error}</p>
      </div>
    );
  }

  if (!graph) {
    return <p className="label-caps">Loading…</p>;
  }

  const personaCount = graph.nodes.filter((n) => n.kind === "persona").length;
  const showCount = graph.nodes.filter((n) => n.kind === "show").length;

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="font-serif text-2xl text-navy sm:text-[28px]">
          {personaCount} persona{personaCount === 1 ? "" : "s"} listening across {showCount} show
          {showCount === 1 ? "" : "s"}
        </h1>
        <p className="label-caps">
          Built from the persona registry (GET /network) — IDEA_DOC.md §14: personas as agents,
          agents as each other&rsquo;s audience.
        </p>
      </header>
      <NetworkGraphView graph={graph} descriptionsById={descriptionsById} />
    </div>
  );
}
