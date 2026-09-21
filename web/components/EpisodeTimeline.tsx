import type { EpisodeDigest, SkippedEpisode } from "@/lib/api-types";
import {
  highlightTimestamps,
  isHighlightWindow,
  resolveDurationSeconds,
  secondsToClock,
  tickHeightForScore,
  xForTime,
  youtubeDeepLink,
} from "@/lib/timeline";
import { episodeInputLabel } from "@/lib/usage";

const VIEW_WIDTH = 900;
const VIEW_HEIGHT = 56;
const BASELINE_Y = 46;
const MAX_TICK_HEIGHT = 34;
const MAX_HIGHLIGHT_TICK_HEIGHT = 40;

function EpisodeRow({
  episode,
  transcriptSource,
}: {
  episode: EpisodeDigest;
  transcriptSource?: string;
}) {
  const title = episode.episode_title ?? episode.episode_id;
  const duration = resolveDurationSeconds(episode);
  const hlTimestamps = highlightTimestamps(episode);

  return (
    <div className="border-t border-taupe py-3 first:border-t-0">
      <div className="mb-1.5 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <span className="font-serif text-sm text-ink">
          {title}
          {transcriptSource ? (
            <span className="label-caps ml-2 font-mono normal-case">{transcriptSource}</span>
          ) : null}
        </span>
        <span className="label-caps">
          {episode.refused ? (
            <span className="text-silver">Refused</span>
          ) : (
            <>
              <span className="font-mono">{secondsToClock(duration)}</span>
              {" · "}
              {episode.highlights.length} highlight
              {episode.highlights.length === 1 ? "" : "s"}
            </>
          )}
        </span>
      </div>

      {episode.refused ? (
        <p className="font-serif text-sm italic text-silver">
          {episode.refusal_reason ?? "nothing cleared the relevance bar"}
        </p>
      ) : (
        <svg
          viewBox={`0 0 ${VIEW_WIDTH} ${VIEW_HEIGHT}`}
          className="h-14 w-full"
          role="img"
          aria-label={`Relevance timeline for ${title}, ${secondsToClock(duration)} long, ${episode.windows.length} scored windows`}
          preserveAspectRatio="none"
        >
          <line
            x1={0}
            y1={BASELINE_Y}
            x2={VIEW_WIDTH}
            y2={BASELINE_Y}
            stroke="var(--taupe)"
            strokeWidth={1}
          />
          {episode.windows.map((w, i) => {
            const x = xForTime(w.start, duration, VIEW_WIDTH);
            const surfaced = isHighlightWindow(w.start, hlTimestamps);
            const maxHeight = surfaced ? MAX_HIGHLIGHT_TICK_HEIGHT : MAX_TICK_HEIGHT;
            const height = tickHeightForScore(w.score, maxHeight);
            const color = surfaced ? "var(--blue)" : "var(--navy)";
            const label = `${secondsToClock(w.start)} · score ${w.score.toFixed(2)}${
              surfaced ? " · surfaced highlight" : ""
            }`;
            const href = youtubeDeepLink(episode.episode_id, w.start);
            const tick = (
              <>
                <title>{label}</title>
                <rect
                  x={x - (surfaced ? 1.5 : 1)}
                  y={BASELINE_Y - height}
                  width={surfaced ? 3 : 2}
                  height={height}
                  fill={color}
                />
              </>
            );
            const key = `${episode.episode_id}-${i}-${w.start}`;
            return href ? (
              <a
                key={key}
                href={href}
                target="_blank"
                rel="noopener noreferrer"
                aria-label={label}
                className="timeline-tick"
              >
                {tick}
              </a>
            ) : (
              <g key={key} tabIndex={0} role="img" aria-label={label} className="timeline-tick">
                {tick}
              </g>
            );
          })}
        </svg>
      )}
    </div>
  );
}

export function EpisodeTimeline({
  episodes,
  transcriptSources,
  skipped,
}: {
  episodes: EpisodeDigest[];
  /** usage.transcript_sources (Phase C): resolved episode id -> provider
   * ("fixture", "supadata", "rss:json", "deepgram", ...). */
  transcriptSources?: Record<string, string>;
  /** usage.skipped (Phase C): episodes the server could not resolve a
   * transcript for, with why. */
  skipped?: SkippedEpisode[];
}) {
  return (
    <section aria-label="Episode timeline strip">
      <div>
        {episodes.map((ep) => (
          <EpisodeRow
            key={ep.episode_id}
            episode={ep}
            transcriptSource={transcriptSources?.[ep.episode_id]}
          />
        ))}
      </div>
      {skipped && skipped.length > 0 ? (
        <div className="mt-3 border-t border-taupe pt-3">
          <p className="label-caps mb-1">Skipped</p>
          <ul className="space-y-1">
            {skipped.map((s, i) => (
              <li key={i} className="font-serif text-sm italic text-silver">
                <span className="font-mono not-italic">{episodeInputLabel(s.episode)}</span>
                {" — "}
                {s.reason}
              </li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}
