import { readFileSync } from "node:fs";
import path from "node:path";

import { describe, expect, it } from "vitest";

import type { PodcastSearchResult, Source, Subscription, SubscriptionCreateRequest } from "@/lib/api-types";
import {
  applyRun,
  applySkippedRun,
  applyUpdate,
  buildMockPreview,
  decodeXmlEntities,
  fakeId,
  looksLikeOpml,
  nextRunAt,
  parseOpml,
  placeholderArtworkSvg,
  planRun,
  sourceReadError,
  renderSoulFromInterview,
  resolveLink,
  searchCatalog,
  subscriptionFromRequest,
  validateCreateRequest,
} from "./mock-subscribe";

const MOCKS_DIR = path.join(process.cwd(), "mocks");
const catalog = JSON.parse(readFileSync(path.join(MOCKS_DIR, "podcast_catalog.json"), "utf-8")) as PodcastSearchResult[];
const sampleOpml = readFileSync(path.join(MOCKS_DIR, "subscriptions_sample.opml"), "utf-8");

const NOW = new Date("2026-10-02T15:30:00Z"); // a Friday

describe("searchCatalog", () => {
  it("returns nothing for a blank query", () => {
    expect(searchCatalog(catalog, "   ")).toEqual([]);
  });

  it("ranks title prefix before title substring before author", () => {
    const results = searchCatalog(catalog, "acq");
    expect(results[0].title).toBe("Acquired");
    const byAuthor = searchCatalog(catalog, "seides");
    expect(byAuthor.map((r) => r.title)).toEqual(["Capital Allocators"]);
    const substring = searchCatalog(catalog, "money");
    expect(substring.map((r) => r.title)).toEqual(["Planet Money"]);
  });

  it("honors the limit and is case-insensitive", () => {
    expect(searchCatalog(catalog, "THE", 2).length).toBeLessThanOrEqual(2);
    expect(searchCatalog(catalog, "ODD LOTS")[0].title).toBe("Odd Lots");
  });

  it("catalog entries have the contract's fields", () => {
    for (const entry of catalog) {
      expect(entry.title).toBeTruthy();
      expect(entry.feed_url).toMatch(/^https:\/\//);
      expect(entry).toHaveProperty("artwork_url");
      expect(typeof entry.apple_id).toBe("number");
    }
  });
});

describe("resolveLink", () => {
  it("recognizes a catalog RSS url and returns its title", () => {
    const outcome = resolveLink("https://feeds.transistor.fm/acquired", catalog);
    expect(outcome).toMatchObject({ ok: true, source: { kind: "rss", title: "Acquired" } });
  });

  it("recognizes an unknown but feed-shaped url", () => {
    const outcome = resolveLink("https://example.com/podcast/feed.xml", catalog);
    expect(outcome.ok).toBe(true);
    if (outcome.ok) expect(outcome.source).toMatchObject({ kind: "rss", feed_url: "https://example.com/podcast/feed.xml" });
  });

  it("recognizes an Apple Podcasts show url, using the catalog when it knows the id", () => {
    const outcome = resolveLink("https://podcasts.apple.com/us/podcast/acquired/id1050462261", catalog);
    expect(outcome).toMatchObject({
      ok: true,
      source: { kind: "rss", feed_url: "https://feeds.transistor.fm/acquired", title: "Acquired" },
    });
  });

  it("derives a title for an unknown Apple show", () => {
    const outcome = resolveLink("https://podcasts.apple.com/us/podcast/the-daily-brief/id9999999999", catalog);
    expect(outcome).toMatchObject({ ok: true, source: { kind: "rss", title: "The Daily Brief" } });
  });

  it("rejects an Apple link with no show id", () => {
    const outcome = resolveLink("https://podcasts.apple.com/us/browse", catalog);
    expect(outcome.ok).toBe(false);
  });

  it("recognizes YouTube channel ids and handles", () => {
    const byId = resolveLink("https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv", catalog);
    expect(byId).toMatchObject({ ok: true, source: { kind: "youtube", channel_id: "UCabcdefghijklmnopqrstuv" } });
    const byHandle = resolveLink("https://youtube.com/@AcquiredFM", catalog);
    expect(byHandle.ok).toBe(true);
    if (byHandle.ok && byHandle.source.kind === "youtube") {
      expect(byHandle.source.channel_id).toMatch(/^UC[\w-]{22}$/);
      // Stable: the same handle always maps to the same channel id.
      const again = resolveLink("https://youtube.com/@acquiredfm", catalog);
      expect(again.ok && again.source.kind === "youtube" && again.source.channel_id).toBe(byHandle.source.channel_id);
    }
  });

  it("rejects a YouTube video link as not a channel", () => {
    const outcome = resolveLink("https://www.youtube.com/watch?v=abc123", catalog);
    expect(outcome.ok).toBe(false);
  });

  it("returns an explanatory detail for anything else", () => {
    const outcome = resolveLink("https://example.com/about", catalog);
    expect(outcome.ok).toBe(false);
    if (!outcome.ok) expect(outcome.detail).toMatch(/RSS feed/);
  });

  it("rejects blank, malformed, and non-http input", () => {
    expect(resolveLink("   ", catalog).ok).toBe(false);
    expect(resolveLink("ftp://example.com/feed.xml", catalog).ok).toBe(false);
    expect(resolveLink("http://", catalog).ok).toBe(false);
  });

  it("accepts a scheme-less address", () => {
    expect(resolveLink("feeds.megaphone.fm/investlikethebest", catalog)).toMatchObject({
      ok: true,
      source: { title: "Invest Like the Best" },
    });
  });
});

describe("OPML", () => {
  it("decodes XML entities", () => {
    expect(decodeXmlEntities("News &amp; Economics &#8212; &#x41;")).toBe("News & Economics — A");
    expect(decodeXmlEntities("&unknown;")).toBe("&unknown;");
  });

  it("recognizes OPML", () => {
    expect(looksLikeOpml(sampleOpml)).toBe(true);
    expect(looksLikeOpml("<html><body>nope</body></html>")).toBe(false);
    expect(looksLikeOpml("")).toBe(false);
  });

  it("imports every feed in the committed sample, through folders", () => {
    const { sources, skipped } = parseOpml(sampleOpml);
    expect(sources.map((s) => (s.kind === "rss" ? s.title : null))).toEqual([
      "Acquired",
      "Invest Like the Best",
      "Capital Allocators",
      "Business Breakdowns",
      "Odd Lots",
      "Planet Money",
      "Marketplace",
      "Hidden Forces",
      "Acquired (duplicate entry)",
      "Radiolab for Kids & Families",
    ]);
    expect(skipped).toHaveLength(2);
    expect(skipped[0].reason).toMatch(/xmlUrl/);
    expect(skipped[1].reason).toMatch(/not http/);
    // Skipped entries carry 1-based line numbers that point at the right line.
    const lines = sampleOpml.split("\n");
    for (const s of skipped) {
      expect(typeof s.line).toBe("number");
      expect(lines[(s.line as number) - 1]).toMatch(/<outline/);
    }
  });

  it("does not report folders as skipped", () => {
    const { sources, skipped } = parseOpml(
      '<opml><body><outline text="Folder"><outline xmlUrl="https://a.test/feed"/></outline></body></opml>',
    );
    expect(sources).toHaveLength(1);
    expect(skipped).toEqual([]);
  });

  it("handles single-quoted attributes and one-line documents", () => {
    const { sources } = parseOpml(
      "<opml><body><outline text='A &amp; B' xmlUrl='https://a.test/feed'/><outline title=\"C\" xmlUrl=\"https://c.test/rss\"/></body></opml>",
    );
    expect(sources).toEqual([
      { kind: "rss", feed_url: "https://a.test/feed", title: "A & B", artwork_url: null },
      { kind: "rss", feed_url: "https://c.test/rss", title: "C", artwork_url: null },
    ]);
  });
});

describe("renderSoulFromInterview", () => {
  it("fills the six-section template from the six answer keys", () => {
    const soul = renderSoulFromInterview({
      identity: "Portfolio manager",
      interests: "rates, energy",
      triggers: "a number with a mechanism",
      ignore: "celebrity, hype",
      style: "empirical",
      guidance: "High bar.",
    });
    expect(soul).toBe(
      [
        "# Soul (interview)",
        "",
        "## Identity & Role",
        "Portfolio manager",
        "",
        "## Core Interests",
        "- rates",
        "- energy",
        "",
        "## Attention Triggers",
        "- a number with a mechanism",
        "",
        "## Anti-interests",
        "- celebrity",
        "- hype",
        "",
        "## Taste & Sensibility",
        "empirical",
        "",
        "## Curation Guidance",
        "High bar.",
        "",
      ].join("\n"),
    );
  });

  it("falls back the way build_from_interview does", () => {
    const soul = renderSoulFromInterview({ interests: "rates, energy" });
    expect(soul).toContain("## Identity & Role\nStated by the principal.");
    expect(soul).toContain("## Attention Triggers\n- rates\n- energy");
    expect(soul).toContain("## Anti-interests\n- (none inferred)");
    expect(soul).toContain("## Taste & Sensibility\nAs stated.");
    expect(soul).toContain("Surface segments matching the triggers; high bar for everything else.");
  });
});

describe("buildMockPreview", () => {
  const sources: Source[] = [
    { kind: "rss", feed_url: "https://feeds.transistor.fm/acquired", title: "Acquired", artwork_url: null },
    { kind: "rss", feed_url: "https://feeds.megaphone.fm/investlikethebest", title: "Invest Like the Best", artwork_url: null },
    { kind: "youtube", channel_id: "UCabcdefghijklmnopqrstuv", title: "A Channel" },
  ];

  it("is deterministic and stays inside the lookback window, newest first", () => {
    const a = buildMockPreview(sources, 7, 20, NOW);
    const b = buildMockPreview(sources, 7, 20, NOW);
    expect(a).toEqual(b);
    expect(a.episodes.length).toBeGreaterThanOrEqual(sources.length);
    for (const ep of a.episodes) {
      const age = NOW.getTime() - Date.parse(ep.published_at);
      expect(age).toBeGreaterThan(0);
      expect(age).toBeLessThan(7 * 86_400_000);
    }
    const times = a.episodes.map((e) => Date.parse(e.published_at));
    expect([...times].sort((x, y) => y - x)).toEqual(times);
  });

  it("caps total episodes at the per-run maximum", () => {
    expect(buildMockPreview(sources, 7, 2, NOW).episodes).toHaveLength(2);
  });

  it("reports unreachable sources as errors, not episodes", () => {
    const broken: Source = {
      kind: "rss",
      feed_url: "https://unreachable.example.com/feed",
      title: "Broken",
      artwork_url: null,
    };
    const result = buildMockPreview([broken, ...sources], 7, 20, NOW);
    expect(result.errors).toEqual([{ source: broken, reason: expect.stringContaining("did not respond") }]);
    expect(result.episodes.every((e) => e.source_title !== "Broken")).toBe(true);
  });

  it("fills a usable episode reference per kind", () => {
    const { episodes } = buildMockPreview(sources, 7, 20, NOW);
    const rss = episodes.find((e) => e.source_title === "Acquired");
    expect(rss?.episode.feed_url).toBe("https://feeds.transistor.fm/acquired");
    expect(rss?.episode.guid).toBeTruthy();
    const yt = episodes.find((e) => e.source_title === "A Channel");
    expect(yt?.episode.video_id).toHaveLength(11);
  });
});

describe("source read errors", () => {
  const http: Source = { kind: "rss", feed_url: "http://feeds.example.com/plain", title: "Plain", artwork_url: null };

  it("http:// feeds are reported per source in the preview, not fetched", () => {
    const ok: Source = { kind: "rss", feed_url: "https://feeds.transistor.fm/acquired", title: "Acquired", artwork_url: null };
    const result = buildMockPreview([http, ok], 7, 20, NOW);
    expect(result.errors).toEqual([{ source: http, reason: expect.stringContaining("https://") }]);
    expect(result.episodes.every((e) => e.source_title === "Acquired")).toBe(true);
  });

  it("sourceReadError is null for a healthy https feed and a youtube channel", () => {
    expect(sourceReadError({ ...http, feed_url: "https://feeds.example.com/ok" })).toBeNull();
    expect(sourceReadError({ kind: "youtube", channel_id: "UCabc", title: null })).toBeNull();
    expect(sourceReadError(http)).not.toBeNull();
  });

  it("rss preview episodes carry exactly feed_url, guid, audio_url", () => {
    const ok: Source = { kind: "rss", feed_url: "https://feeds.transistor.fm/acquired", title: "Acquired", artwork_url: null };
    const { episodes } = buildMockPreview([ok], 7, 20, NOW);
    expect(episodes.length).toBeGreaterThan(0);
    for (const ep of episodes) {
      expect(Object.keys(ep.episode).sort()).toEqual(["audio_url", "feed_url", "guid"]);
      expect(ep.episode.audio_url).toMatch(/^https:\/\//);
    }
  });
});

describe("planRun", () => {
  const ok: Source = { kind: "rss", feed_url: "https://feeds.transistor.fm/acquired", title: "Acquired", artwork_url: null };
  const bad: Source = { kind: "rss", feed_url: "http://feeds.example.com/x", title: "Bad", artwork_url: null };
  const make = (sources: Source[]): Subscription =>
    subscriptionFromRequest(
      {
        email: "a@b.co",
        soul: "# Soul",
        context: "",
        sources,
        cadence: "weekly",
        highlight_count: 4,
        profile: null,
        max_episodes_per_run: 5,
        notify_when_empty: false,
      },
      "s",
      "o",
      NOW,
    );

  it("starts a job for a subscription that has not run", () => {
    expect(planRun(make([ok]), NOW)).toEqual({ kind: "job", newEpisodes: 1 });
  });

  it("returns a null-job reason when the last run was seconds ago", () => {
    const ran = applyRun(make([ok]), "j", 1, NOW);
    expect(planRun(ran, new Date(NOW.getTime() + 5_000))).toEqual({ kind: "skipped", reason: "no new episodes" });
    expect(planRun(ran, new Date(NOW.getTime() + 120_000)).kind).toBe("job");
  });

  it("names unreadable sources in the reason, and skips when none can be read", () => {
    const ran = applyRun(make([ok, bad]), "j", 1, NOW);
    expect(planRun(ran, new Date(NOW.getTime() + 5_000))).toEqual({
      kind: "skipped",
      reason: "no new episodes; 1 of 2 source(s) could not be read",
    });
    expect(planRun(make([bad]), NOW)).toEqual({
      kind: "skipped",
      reason: "no new episodes; 1 of 1 source(s) could not be read",
    });
  });

  it("records a skipped run without a job", () => {
    const skipped = applySkippedRun(make([ok]), "no new episodes", NOW);
    expect(skipped.last_run_summary).toEqual({
      ran_at: NOW.toISOString(),
      new_episodes: 0,
      job_id: null,
      skipped_reason: "no new episodes",
    });
    expect(skipped.last_job_id).toBeNull();
  });
});

describe("nextRunAt", () => {
  it("weekly lands on a Friday 12:00 UTC strictly after now", () => {
    // NOW is Friday 15:30 UTC, past noon, so the next one is a week out.
    expect(nextRunAt("weekly", NOW).toISOString()).toBe("2026-10-09T12:00:00.000Z");
    // Friday morning: later the same day.
    expect(nextRunAt("weekly", new Date("2026-10-02T08:00:00Z")).toISOString()).toBe("2026-10-02T12:00:00.000Z");
    // Midweek.
    expect(nextRunAt("weekly", new Date("2026-10-05T09:00:00Z")).toISOString()).toBe("2026-10-09T12:00:00.000Z");
    for (const d of [0, 1, 2, 3, 4, 5, 6]) {
      const from = new Date(Date.UTC(2026, 9, 4 + d, 18, 0));
      const next = nextRunAt("weekly", from);
      expect(next.getUTCDay()).toBe(5);
      expect(next.getTime()).toBeGreaterThan(from.getTime());
    }
  });

  it("daily is the next 12:00 UTC", () => {
    expect(nextRunAt("daily", NOW).toISOString()).toBe("2026-10-03T12:00:00.000Z");
    expect(nextRunAt("daily", new Date("2026-10-02T06:00:00Z")).toISOString()).toBe("2026-10-02T12:00:00.000Z");
  });
});

describe("subscriptions", () => {
  const body: SubscriptionCreateRequest = {
    email: "reader@example.com",
    soul: "# Soul",
    context: "ctx",
    sources: [
      { kind: "rss", feed_url: "https://feeds.transistor.fm/acquired", title: "Acquired", artwork_url: null },
    ],
    cadence: "weekly",
    highlight_count: 4,
    profile: null,
    max_episodes_per_run: 5,
    notify_when_empty: false,
  };

  it("validates create requests", () => {
    expect(validateCreateRequest(body)).toBeNull();
    expect(validateCreateRequest({ ...body, email: "nope" })).toMatch(/email/);
    expect(validateCreateRequest({ ...body, soul: " " })).toMatch(/soul/);
    expect(validateCreateRequest({ ...body, sources: [] })).toMatch(/source/);
    expect(validateCreateRequest({ ...body, sources: [{ kind: "rss" } as unknown as Source] })).toMatch(/source/);
    expect(validateCreateRequest({ ...body, cadence: "monthly" as never })).toMatch(/cadence/);
  });

  it("builds a Subscription with server-assigned fields", () => {
    const sub = subscriptionFromRequest(body, "sub_1", "owner@example.com", NOW);
    expect(sub).toMatchObject({
      subscription_id: "sub_1",
      owner: "owner@example.com",
      email: "reader@example.com",
      active: true,
      shows: null,
      episodes: null,
      profile: null,
      last_run_summary: null,
      seen_episode_ids: [],
      next_run_at: "2026-10-09T12:00:00.000Z",
      created_at: NOW.toISOString(),
    });
  });

  it("applies a patch, re-planning on resume and cadence change only", () => {
    const sub = subscriptionFromRequest(body, "sub_1", "o", NOW);
    const paused = applyUpdate(sub, { active: false }, NOW);
    expect(paused.active).toBe(false);
    expect(paused.next_run_at).toBe(sub.next_run_at);

    const later = new Date("2026-10-12T10:00:00Z");
    const resumed = applyUpdate(paused, { active: true }, later);
    expect(resumed.next_run_at).toBe("2026-10-16T12:00:00.000Z");

    const daily = applyUpdate(sub, { cadence: "daily", context: "new" }, NOW);
    expect(daily).toMatchObject({ cadence: "daily", context: "new", next_run_at: "2026-10-03T12:00:00.000Z" });
  });

  it("replacing sources clears legacy shows and episodes and de-duplicates", () => {
    const sub: Subscription = {
      ...subscriptionFromRequest(body, "s", "o", NOW),
      shows: ["Legacy"],
      episodes: [{ video_id: "abc" }],
      sources: null,
    };
    const dup = body.sources[0];
    const next = applyUpdate(sub, { sources: [dup, { ...dup }] }, NOW);
    expect(next.shows).toBeNull();
    expect(next.episodes).toBeNull();
    expect(next.sources).toHaveLength(1);
  });

  it("only changes the fields that were sent", () => {
    const sub: Subscription = {
      ...subscriptionFromRequest(body, "s", "o", NOW),
      shows: ["Legacy"],
      episodes: [{ video_id: "abc" }],
    };
    const next = applyUpdate(sub, { context: "new" }, NOW);
    expect(next.shows).toEqual(["Legacy"]);
    expect(next.episodes).toEqual([{ video_id: "abc" }]);
    expect(next.sources).toEqual(sub.sources);
  });

  it("records a run", () => {
    const sub = subscriptionFromRequest(body, "sub_1", "o", NOW);
    const ran = applyRun(sub, "job123", 3, NOW);
    expect(ran.last_job_id).toBe("job123");
    expect(ran.last_run_summary).toEqual({
      ran_at: NOW.toISOString(),
      new_episodes: 3,
      job_id: "job123",
      skipped_reason: null,
    });
  });
});

describe("artwork placeholder", () => {
  it("escapes markup in the title", () => {
    expect(placeholderArtworkSvg("<script>")).not.toContain("<script>");
    expect(placeholderArtworkSvg("Odd Lots")).toContain(">OL<");
  });

  it("fakeId is stable and the requested length", () => {
    expect(fakeId("x", 11)).toBe(fakeId("x", 11));
    expect(fakeId("x", 11)).toHaveLength(11);
    expect(fakeId("x", 11)).not.toBe(fakeId("y", 11));
  });
});
