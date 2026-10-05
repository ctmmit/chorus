import type { Digest } from "@/lib/api-types";
import { STANCE_LABEL, isDisagreement, threadRows } from "@/lib/threads";

/** Questions two or more sources spoke to (chorus/threads.py), above the
 * per-source highlights: that conversation is the headline of the week. */
export function ThreadsSection({ digest }: { digest: Digest }) {
  const threads = digest.threads ?? [];
  if (threads.length === 0) return null;

  return (
    <section className="space-y-4" aria-label="Threads across sources">
      <h2 className="label-caps">Threads across sources</h2>
      {threads.map((thread, i) => {
        const rows = threadRows(thread, digest);
        return (
          <article key={i} className="border-t border-taupe pt-3 first:border-t-0">
            <h3 className="font-serif text-lg text-navy">
              {thread.question}
              {isDisagreement(thread) ? (
                <span className="label-caps ml-2 align-middle text-blue">Disagreement</span>
              ) : null}
            </h3>
            <ul className="mt-2 space-y-2">
              {rows.map((row) => (
                <li key={row.key} className="grid grid-cols-[6.5rem_1fr] gap-3">
                  <span className="label-caps pt-0.5 text-silver">{STANCE_LABEL[row.stance]}</span>
                  <div>
                    <blockquote className="font-serif text-[15px] leading-relaxed text-ink">
                      “{row.quote}”
                    </blockquote>
                    <p className="mt-1 font-sans text-xs text-silver">
                      {row.source}
                      {row.remembered ? (
                        <span className="italic"> · surfaced {row.remembered}</span>
                      ) : null}
                      {row.clock ? (
                        row.href ? (
                          <>
                            {" · "}
                            <a href={row.href} target="_blank" rel="noopener noreferrer" className="text-blue underline">
                              <span className="font-mono">{row.clock}</span>
                            </a>
                          </>
                        ) : (
                          <span className="font-mono"> · {row.clock}</span>
                        )
                      ) : null}
                    </p>
                  </div>
                </li>
              ))}
            </ul>
          </article>
        );
      })}
    </section>
  );
}
