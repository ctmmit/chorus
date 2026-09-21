import type { Highlight } from "@/lib/api-types";
import { secondsToClock, youtubeDeepLink } from "@/lib/timeline";

export function HighlightCard({ highlight }: { highlight: Highlight }) {
  const href = youtubeDeepLink(highlight.episode_id, highlight.segment_timestamp);
  const time = secondsToClock(highlight.segment_timestamp);

  return (
    <article className="border-t border-taupe py-4 first:border-t-0">
      <blockquote className="font-serif text-[15px] leading-relaxed text-ink">
        “{highlight.quote}”
      </blockquote>
      <p className="mt-2 font-serif text-sm italic text-silver">{highlight.why_surface}</p>
      <div className="mt-2 flex flex-wrap items-baseline gap-x-3 gap-y-1 font-sans text-xs">
        <span className="font-mono text-navy">{highlight.relevance_score.toFixed(2)}</span>
        <span className="text-silver">{highlight.episode_title ?? highlight.episode_id}</span>
        {href ? (
          <a href={href} target="_blank" rel="noopener noreferrer" className="text-blue underline">
            <span className="font-mono">{time}</span> · watch
          </a>
        ) : (
          <span className="font-mono text-silver">{time}</span>
        )}
      </div>
    </article>
  );
}
