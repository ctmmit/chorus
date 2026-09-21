"use client";

import { useEffect, useState } from "react";

import { ApiError, fetchPersona } from "@/lib/api-client";
import type { Persona } from "@/lib/api-types";
import { getBaseUrl, getToken } from "@/lib/storage";

function formatCreatedAt(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  // DD MMM YYYY, uppercase month (Fulcrum masthead date convention).
  const day = String(date.getUTCDate()).padStart(2, "0");
  const month = date.toLocaleString("en-US", { month: "short", timeZone: "UTC" }).toUpperCase();
  return `${day} ${month} ${date.getUTCFullYear()}`;
}

export function PersonaView({ personaId }: { personaId: string }) {
  const [persona, setPersona] = useState<Persona | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    const baseUrl = getBaseUrl();
    const token = getToken();

    async function load(): Promise<void> {
      try {
        const result = await fetchPersona(baseUrl, token, personaId);
        if (cancelled) return;
        setPersona(result);
        setError(null);
      } catch (err) {
        if (cancelled) return;
        setError(
          err instanceof ApiError && err.status === 404
            ? "Unknown persona."
            : err instanceof ApiError
              ? err.message
              : "Could not reach the Chorus API.",
        );
      } finally {
        if (!cancelled) setLoading(false);
      }
    }

    void load();
    return () => {
      cancelled = true;
    };
  }, [personaId]);

  if (loading) {
    return <p className="label-caps">Loading…</p>;
  }

  if (error || !persona) {
    return (
      <div className="space-y-2">
        <p className="font-serif text-lg text-ink">{error ?? "Unknown persona."}</p>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <header className="space-y-2">
        <p className="label-caps">
          Persona <span className="font-mono normal-case text-ink">{persona.persona_id}</span>
        </p>
        <h1 className="font-serif text-2xl text-navy sm:text-[28px]">{persona.name}</h1>
        {persona.description ? (
          <p className="font-serif text-[15px] text-ink">{persona.description}</p>
        ) : null}
        <dl className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 font-sans text-xs sm:grid-cols-4">
          <div>
            <dt className="label-caps">Cadence</dt>
            <dd className="font-mono text-ink">{persona.cadence}</dd>
          </div>
          <div>
            <dt className="label-caps">Registered</dt>
            <dd className="font-mono text-ink">{formatCreatedAt(persona.created_at)}</dd>
          </div>
          <div>
            <dt className="label-caps">soul_version</dt>
            <dd className="font-mono text-ink">{persona.soul_version}</dd>
          </div>
          <div>
            <dt className="label-caps">Visibility</dt>
            <dd className="font-mono text-ink">{persona.public ? "public" : "private"}</dd>
          </div>
        </dl>
      </header>

      <section className="space-y-2">
        <h2 className="label-caps">Shows</h2>
        {persona.shows.length === 0 ? (
          <p className="font-serif text-sm italic text-silver">No shows registered yet.</p>
        ) : (
          <ul className="flex flex-wrap gap-x-4 gap-y-1">
            {persona.shows.map((show) => (
              <li key={show} className="font-serif text-sm text-ink">
                {show}
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="space-y-2">
        <h2 className="label-caps">Soul</h2>
        <pre className="whitespace-pre-wrap border-t border-taupe pt-3 font-serif text-sm leading-relaxed text-ink">
          {persona.soul}
        </pre>
      </section>

      <section className="space-y-2 border-t border-taupe pt-3">
        <h2 className="label-caps">Discovery documents</h2>
        <ul className="space-y-1 font-mono text-sm text-blue">
          <li>/personas/{persona.persona_id}/agent.json</li>
          <li>/personas/{persona.persona_id}/agent-facts.json</li>
        </ul>
      </section>
    </div>
  );
}
