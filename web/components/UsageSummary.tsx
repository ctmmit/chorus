import type { JobUsage } from "@/lib/api-types";
import { cacheReadSharePercent, orderedStages } from "@/lib/usage";

const STAGE_LABEL: Record<string, string> = {
  ingest: "ingest",
  curate: "curate",
  script: "script",
  audio: "audio",
};

/** Compact run-telemetry line under the provenance line: stage seconds and
 * (when reported) LLM token spend. Fulcrum register: silver labels, mono
 * numerics, no decoration — this is a meter, not a callout. */
export function UsageSummary({ usage }: { usage: JobUsage }) {
  const stages = orderedStages(usage.stage_seconds);
  const tokens = usage.llm_tokens;
  const cacheShare = tokens ? cacheReadSharePercent(tokens) : null;

  if (stages.length === 0 && !tokens) return null;

  return (
    <p className="label-caps flex flex-wrap gap-x-4 gap-y-1">
      {stages.map(([stage, seconds]) => (
        <span key={stage}>
          {STAGE_LABEL[stage] ?? stage}{" "}
          <span className="font-mono normal-case text-ink">{seconds.toFixed(1)}s</span>
        </span>
      ))}
      {tokens ? (
        <>
          <span>
            llm calls <span className="font-mono normal-case text-ink">{tokens.calls}</span>
          </span>
          <span>
            tokens in/out{" "}
            <span className="font-mono normal-case text-ink">
              {tokens.input_tokens}/{tokens.output_tokens}
            </span>
          </span>
          {cacheShare !== null ? (
            <span>
              cache read{" "}
              <span className="font-mono normal-case text-ink">{cacheShare.toFixed(0)}%</span>
            </span>
          ) : null}
        </>
      ) : null}
    </p>
  );
}
