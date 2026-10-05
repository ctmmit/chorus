/**
 * Display rows for cross-source threads (chorus/threads.py) and chapters
 * (chorus/chapters.py). Pure, so the job view stays a thin renderer.
 */
import type { Chapter, Digest, Highlight, Stance, Thread } from "./api-types";
import { secondsToClock, youtubeDeepLink } from "./timeline";

export const STANCE_LABEL: Record<Stance, string> = {
  agrees: "Agrees",
  disagrees: "Disagrees",
  adds: "Adds",
};

export interface ThreadRow {
  key: string;
  stance: Stance;
  source: string;
  quote: string;
  /** "12:34" for this week's highlights; null for a remembered claim. */
  clock: string | null;
  href: string | null;
  /** "14 Sep 2026" when the claim came from an earlier digest. */
  remembered: string | null;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** DD MMM YYYY in UTC, the house date format. */
export function formatDay(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const day = String(d.getUTCDate()).padStart(2, "0");
  return `${day} ${MONTHS[d.getUTCMonth()]} ${d.getUTCFullYear()}`;
}

function highlightsById(digest: Digest): Map<string, Highlight> {
  const map = new Map<string, Highlight>();
  for (const ep of digest.episodes) {
    for (const h of ep.highlights) {
      if (h.highlight_id) map.set(h.highlight_id, h);
    }
  }
  return map;
}

/** One row per member; a member whose highlight is not in this digest and
 * that is not a remembered claim is dropped (it cannot be shown honestly). */
export function threadRows(thread: Thread, digest: Digest): ThreadRow[] {
  const byId = highlightsById(digest);
  const rows: ThreadRow[] = [];
  for (const m of thread.members) {
    if (m.remembered_at) {
      rows.push({
        key: m.highlight_id,
        stance: m.stance,
        source: m.source ?? m.episode_id,
        quote: m.quote ?? "",
        clock: null,
        href: null,
        remembered: formatDay(m.remembered_at),
      });
      continue;
    }
    const h = byId.get(m.highlight_id);
    if (!h) continue;
    rows.push({
      key: m.highlight_id,
      stance: m.stance,
      source: h.episode_title ?? h.episode_id,
      quote: h.quote,
      clock: secondsToClock(h.segment_timestamp),
      href: youtubeDeepLink(h.episode_id, h.segment_timestamp),
      remembered: null,
    });
  }
  return rows;
}

export function isDisagreement(thread: Thread): boolean {
  return thread.members.some((m) => m.stance === "disagrees");
}

export interface ChapterRow {
  key: string;
  clock: string;
  title: string;
  href: string | null;
}

export function chapterRows(chapters: Chapter[] | undefined): ChapterRow[] {
  return (chapters ?? []).map((c, i) => ({
    key: `${i}-${c.start_seconds}`,
    clock: secondsToClock(c.start_seconds),
    title: c.title,
    href: c.url,
  }));
}
