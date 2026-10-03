import { describe, expect, it } from "vitest";

import {
  INTERVIEW_KEYS,
  TWO_HOST_PROFILE,
  type PreviewEpisode,
  type Source,
  type Subscription,
} from "./api-types";
import {
  buildSubscriptionRequest,
  cleanInterviewAnswers,
  clampStep,
  defaultDraft,
  describeLastRun,
  formatDate,
  formatDateTime,
  fragmentForStep,
  furthestReachableStep,
  groupPreviewBySource,
  initialsFor,
  insecureFeedSources,
  interpretRun,
  skippedRunMessage,
  INTERVIEW_QUESTIONS,
  interviewHasAnswers,
  isAuthStatus,
  isValidEmail,
  lookbackDaysFor,
  maskToken,
  mergeSources,
  normalizeFeedUrl,
  parseDraft,
  parseFragmentStep,
  parseFragmentToken,
  previewErrorLabel,
  removeSource,
  serializeDraft,
  skippedLineLabel,
  sourceKey,
  sourceTitle,
  subscriptionSources,
  summarizeSourceTitles,
  validateStep,
  type SubscribeDraft,
} from "./subscribe";

const ACQUIRED: Source = {
  kind: "rss",
  feed_url: "https://feeds.transistor.fm/acquired",
  title: "Acquired",
  artwork_url: null,
};
const ODD_LOTS: Source = {
  kind: "rss",
  feed_url: "https://feeds.example.com/oddlots",
  title: "Odd Lots",
  artwork_url: null,
};
const CHANNEL: Source = { kind: "youtube", channel_id: "UC123", title: "Some Channel" };
const SHOW: Source = { kind: "show", show: "All-In" };

function completeDraft(): SubscribeDraft {
  return {
    ...defaultDraft(),
    email: "reader@example.com",
    sources: [ACQUIRED],
    soul: "# Soul (interview)\n\n## Identity & Role\nAn investor.\n",
  };
}

describe("sources", () => {
  it("titles fall back to identifiers", () => {
    expect(sourceTitle(ACQUIRED)).toBe("Acquired");
    expect(sourceTitle({ ...ACQUIRED, title: null })).toBe(ACQUIRED.kind === "rss" ? ACQUIRED.feed_url : "");
    expect(sourceTitle({ ...CHANNEL, title: "  " })).toBe("UC123");
    expect(sourceTitle(SHOW)).toBe("All-In");
  });

  it("keys identify feeds across scheme, case, trailing slash, and fragment", () => {
    const a = sourceKey({ ...ACQUIRED, feed_url: "http://Feeds.Transistor.FM/acquired/" });
    const b = sourceKey({ ...ACQUIRED, feed_url: "https://feeds.transistor.fm/acquired#top" });
    expect(a).toBe(b);
    expect(sourceKey(ODD_LOTS)).not.toBe(sourceKey(ACQUIRED));
  });

  it("keys never collide across kinds", () => {
    const keys = new Set([ACQUIRED, CHANNEL, SHOW].map(sourceKey));
    expect(keys.size).toBe(3);
    expect(sourceKey({ kind: "show", show: " all-in " })).toBe(sourceKey(SHOW));
  });

  it("normalizeFeedUrl tolerates garbage", () => {
    expect(normalizeFeedUrl("  Not A URL ")).toBe("not a url");
  });

  it("mergeSources appends new and counts duplicates, including inside the batch", () => {
    const result = mergeSources(
      [ACQUIRED],
      [{ ...ACQUIRED, title: "Acquired (dup)" }, ODD_LOTS, ODD_LOTS, CHANNEL],
    );
    expect(result.added).toBe(2);
    expect(result.duplicates).toBe(2);
    expect(result.merged.map(sourceTitle)).toEqual(["Acquired", "Odd Lots", "Some Channel"]);
  });

  it("mergeSources does not mutate its inputs", () => {
    const existing = [ACQUIRED];
    mergeSources(existing, [ODD_LOTS]);
    expect(existing).toHaveLength(1);
  });

  it("removeSource drops by key", () => {
    expect(removeSource([ACQUIRED, ODD_LOTS], sourceKey(ACQUIRED))).toEqual([ODD_LOTS]);
  });

  it("summarizeSourceTitles caps the list", () => {
    expect(summarizeSourceTitles([])).toBe("No shows");
    expect(summarizeSourceTitles([ACQUIRED, ODD_LOTS])).toBe("Acquired, Odd Lots");
    expect(summarizeSourceTitles([ACQUIRED, ODD_LOTS, CHANNEL, SHOW], 2)).toBe(
      "Acquired, Odd Lots +2 more",
    );
  });
});

describe("subscriptionSources", () => {
  const base = { sources: null, shows: null } as unknown as Subscription;

  it("prefers sources, falls back to legacy shows, else empty", () => {
    expect(subscriptionSources({ ...base, sources: [ACQUIRED] })).toEqual([ACQUIRED]);
    expect(subscriptionSources({ ...base, shows: ["All-In"] })).toEqual([SHOW]);
    expect(subscriptionSources({ ...base, sources: [], shows: ["All-In"] })).toEqual([SHOW]);
    expect(subscriptionSources(base)).toEqual([]);
  });
});

describe("validation", () => {
  it("accepts and rejects emails", () => {
    expect(isValidEmail("a@b.co")).toBe(true);
    expect(isValidEmail("  a@b.co  ")).toBe(true);
    expect(isValidEmail("a@b")).toBe(false);
    expect(isValidEmail("a b@c.com")).toBe(false);
    expect(isValidEmail("")).toBe(false);
  });

  it("step 1 needs a token", () => {
    expect(validateStep(1, defaultDraft(), false)).toHaveLength(1);
    expect(validateStep(1, defaultDraft(), true)).toEqual([]);
  });

  it("step 2 needs a source", () => {
    expect(validateStep(2, defaultDraft(), true)).toEqual(["Add at least one show."]);
    expect(validateStep(2, completeDraft(), true)).toEqual([]);
  });

  it("step 3 needs a soul within limits and a bounded context", () => {
    expect(validateStep(3, { ...completeDraft(), soul: "   " }, true)).toHaveLength(1);
    expect(validateStep(3, { ...completeDraft(), soul: "x".repeat(40_001) }, true)).toHaveLength(1);
    expect(validateStep(3, { ...completeDraft(), context: "x".repeat(40_001) }, true)).toHaveLength(1);
    expect(validateStep(3, completeDraft(), true)).toEqual([]);
  });

  it("step 4 needs an email and in-range numbers", () => {
    expect(validateStep(4, completeDraft(), true)).toEqual([]);
    expect(validateStep(4, { ...completeDraft(), email: "nope" }, true)).toHaveLength(1);
    expect(validateStep(4, { ...completeDraft(), highlightCount: 0 }, true)).toHaveLength(1);
    expect(validateStep(4, { ...completeDraft(), highlightCount: 21 }, true)).toHaveLength(1);
    expect(validateStep(4, { ...completeDraft(), maxEpisodesPerRun: 21 }, true)).toHaveLength(1);
    expect(validateStep(4, { ...completeDraft(), maxEpisodesPerRun: 2.5 }, true)).toHaveLength(1);
    expect(validateStep(4, { ...completeDraft(), highlightCount: 20, maxEpisodesPerRun: 1 }, true)).toEqual([]);
  });

  it("furthestReachableStep stops at the first incomplete step", () => {
    expect(furthestReachableStep(defaultDraft(), false)).toBe(1);
    expect(furthestReachableStep(defaultDraft(), true)).toBe(2);
    expect(furthestReachableStep({ ...defaultDraft(), sources: [ACQUIRED] }, true)).toBe(3);
    expect(furthestReachableStep(completeDraft(), true)).toBe(4);
    expect(furthestReachableStep(completeDraft(), false)).toBe(1);
  });

  it("clampStep never skips an incomplete step and bounds garbage", () => {
    expect(clampStep(4, defaultDraft(), true)).toBe(2);
    expect(clampStep(4, completeDraft(), true)).toBe(4);
    expect(clampStep(3, completeDraft(), true)).toBe(3);
    expect(clampStep(99, completeDraft(), true)).toBe(4);
    expect(clampStep(-3, completeDraft(), true)).toBe(1);
    expect(clampStep(Number.NaN, completeDraft(), true)).toBe(1);
  });
});

describe("insecureFeedSources", () => {
  it("flags only http:// RSS feeds", () => {
    const http: Source = { kind: "rss", feed_url: "http://feeds.example.com/a", title: "A", artwork_url: null };
    const upper: Source = { kind: "rss", feed_url: " HTTP://feeds.example.com/b", title: "B", artwork_url: null };
    expect(insecureFeedSources([ACQUIRED, http, CHANNEL, SHOW, upper])).toEqual([http, upper]);
    expect(insecureFeedSources([ACQUIRED])).toEqual([]);
  });
});

describe("interpretRun", () => {
  it("navigates when a job started", () => {
    expect(interpretRun({ job_id: "j1", skipped_reason: null })).toEqual({ kind: "job", jobId: "j1" });
  });

  it("explains a skipped run, with a default when the reason is missing", () => {
    expect(interpretRun({ job_id: null, skipped_reason: "no new episodes; 1 of 3 source(s) could not be read" })).toEqual({
      kind: "skipped",
      reason: "no new episodes; 1 of 3 source(s) could not be read",
    });
    expect(interpretRun({ job_id: null, skipped_reason: null })).toEqual({
      kind: "skipped",
      reason: "no new episodes",
    });
    expect(interpretRun({ job_id: null, skipped_reason: "  " })).toMatchObject({ reason: "no new episodes" });
  });

  it("formats the skipped message as a sentence", () => {
    expect(skippedRunMessage("no new episodes")).toBe("No digest this time: no new episodes.");
    expect(skippedRunMessage("no new episodes.")).toBe("No digest this time: no new episodes.");
  });
});

describe("initialsFor", () => {
  it("takes up to two initials and skips a leading The", () => {
    expect(initialsFor("The Knowledge Project")).toBe("KP");
    expect(initialsFor("Acquired")).toBe("A");
    expect(initialsFor("Invest Like the Best")).toBe("IL");
    expect(initialsFor("   ")).toBe("?");
  });
});

describe("interview questions", () => {
  it("cover exactly the six keys build_from_interview reads", () => {
    expect(Object.keys(INTERVIEW_QUESTIONS).sort()).toEqual(
      ["guidance", "identity", "ignore", "interests", "style", "triggers"],
    );
    expect([...INTERVIEW_KEYS].sort()).toEqual(Object.keys(INTERVIEW_QUESTIONS).sort());
  });

  it("comma-list questions say so", () => {
    for (const key of ["interests", "triggers", "ignore"] as const) {
      expect(INTERVIEW_QUESTIONS[key].hint).toMatch(/comma/i);
    }
  });
});

describe("interview helpers", () => {
  it("detects whether anything was answered", () => {
    expect(interviewHasAnswers({})).toBe(false);
    expect(interviewHasAnswers({ identity: "   " })).toBe(false);
    expect(interviewHasAnswers({ interests: "AI, rates" })).toBe(true);
    expect(interviewHasAnswers({ unrelated: "x" })).toBe(false);
  });

  it("cleanInterviewAnswers trims and drops blanks", () => {
    const answers = { ...defaultDraft().interview, identity: " PM ", style: "empirical", ignore: " " };
    expect(cleanInterviewAnswers(answers)).toEqual({ identity: "PM", style: "empirical" });
  });
});

describe("fragment parsing", () => {
  it("reads the token from the sign-in link fragment", () => {
    expect(parseFragmentToken("#token=abc123")).toBe("abc123");
    expect(parseFragmentToken("token=abc123")).toBe("abc123");
    expect(parseFragmentToken("#step=2&token=abc%2Fdef")).toBe("abc/def");
    expect(parseFragmentToken("#token=%20%20")).toBeNull();
    expect(parseFragmentToken("#step=2")).toBeNull();
    expect(parseFragmentToken("")).toBeNull();
  });

  it("reads and validates the step", () => {
    expect(parseFragmentStep("#step=3")).toBe(3);
    expect(parseFragmentStep("#token=x&step=2")).toBe(2);
    expect(parseFragmentStep("#step=0")).toBeNull();
    expect(parseFragmentStep("#step=5")).toBeNull();
    expect(parseFragmentStep("#step=two")).toBeNull();
    expect(parseFragmentStep("#token=x")).toBeNull();
  });

  it("builds a step fragment", () => {
    expect(fragmentForStep(3)).toBe("#step=3");
  });

  it("masks all but the last four characters of a key", () => {
    expect(maskToken("ck_live_abcdef123456")).toBe("…3456");
    expect(maskToken("abc")).toBe("abc");
  });

  it("flags auth failures", () => {
    expect(isAuthStatus(401)).toBe(true);
    expect(isAuthStatus(403)).toBe(true);
    expect(isAuthStatus(422)).toBe(false);
    expect(isAuthStatus(500)).toBe(false);
  });
});

describe("date formatting", () => {
  it("formats DD MMM YYYY and DD MMM YYYY HH:mm in local time", () => {
    const d = new Date(2026, 9, 2, 14, 5);
    expect(formatDate(d)).toBe("02 Oct 2026");
    expect(formatDateTime(d)).toBe("02 Oct 2026 14:05");
    expect(formatDateTime(new Date(2026, 0, 31, 0, 0))).toBe("31 Jan 2026 00:00");
  });

  it("round-trips an ISO instant through the local zone", () => {
    const d = new Date(2026, 11, 25, 7, 30);
    expect(formatDateTime(d.toISOString())).toBe("25 Dec 2026 07:30");
  });

  it("shows a dash for unparseable input", () => {
    expect(formatDate("not a date")).toBe("—");
    expect(formatDateTime("")).toBe("—");
  });
});

describe("request building", () => {
  it("maps a draft to the POST /subscriptions body (single voice = null profile)", () => {
    const draft: SubscribeDraft = {
      ...completeDraft(),
      email: "  reader@example.com ",
      context: "Reading about rates.",
      cadence: "daily",
      highlightCount: 7,
      maxEpisodesPerRun: 3,
      notifyWhenEmpty: true,
    };
    expect(buildSubscriptionRequest(draft)).toEqual({
      email: "reader@example.com",
      soul: draft.soul,
      context: "Reading about rates.",
      sources: [ACQUIRED],
      cadence: "daily",
      highlight_count: 7,
      profile: null,
      max_episodes_per_run: 3,
      notify_when_empty: true,
    });
  });

  it("two hosts sends the TWO_HOST profile", () => {
    const body = buildSubscriptionRequest({ ...completeDraft(), voice: "two_host" });
    expect(body.profile).toEqual(TWO_HOST_PROFILE);
    expect(body.profile?.format).toBe("dialogue");
    expect(body.profile?.speakers.map((s) => s.role)).toEqual(["host", "cohost"]);
  });

  it("looks back a week for weekly, a day for daily", () => {
    expect(lookbackDaysFor("weekly")).toBe(7);
    expect(lookbackDaysFor("daily")).toBe(1);
  });
});

describe("preview grouping", () => {
  const ep = (source_title: string, title: string, published_at: string): PreviewEpisode => ({
    source_title,
    title,
    published_at,
    episode: { title },
  });

  it("groups by show in first-seen order, newest first within a group", () => {
    const groups = groupPreviewBySource([
      ep("B", "b-old", "2026-09-28T00:00:00Z"),
      ep("A", "a1", "2026-09-29T00:00:00Z"),
      ep("B", "b-new", "2026-10-01T00:00:00Z"),
    ]);
    expect(groups.map((g) => g.source_title)).toEqual(["B", "A"]);
    expect(groups[0].episodes.map((e) => e.title)).toEqual(["b-new", "b-old"]);
  });

  it("returns no groups for no episodes", () => {
    expect(groupPreviewBySource([])).toEqual([]);
  });

  it("labels errors whether the source is text or an object", () => {
    expect(previewErrorLabel({ source: "https://x.test/feed", reason: "404" })).toBe("https://x.test/feed");
    expect(previewErrorLabel({ source: ACQUIRED, reason: "404" })).toBe("Acquired");
  });

  it("labels skipped OPML lines", () => {
    expect(skippedLineLabel(12)).toBe("Line 12");
    expect(skippedLineLabel("<outline text='x'/>")).toBe("<outline text='x'/>");
  });
});

describe("describeLastRun", () => {
  const sub = (over: Partial<Subscription>): Subscription =>
    ({ last_run_summary: null, last_run_at: null, last_job_id: null, ...over }) as Subscription;

  it("no runs", () => {
    expect(describeLastRun(sub({}))).toEqual({ kind: "none" });
  });

  it("a normal run links the summary job", () => {
    expect(
      describeLastRun(
        sub({
          last_run_summary: { ran_at: "2026-10-02T12:00:00Z", new_episodes: 4, job_id: "j1", skipped_reason: null },
        }),
      ),
    ).toEqual({ kind: "ran", ranAt: "2026-10-02T12:00:00Z", newEpisodes: 4, jobId: "j1" });
  });

  it("falls back to last_job_id when the summary has no job", () => {
    const view = describeLastRun(
      sub({
        last_job_id: "j9",
        last_run_summary: { ran_at: "2026-10-02T12:00:00Z", new_episodes: 0, job_id: null, skipped_reason: null },
      }),
    );
    expect(view).toMatchObject({ kind: "ran", jobId: "j9" });
  });

  it("a skipped run reports the reason", () => {
    expect(
      describeLastRun(
        sub({
          last_run_summary: {
            ran_at: "2026-10-02T12:00:00Z",
            new_episodes: 0,
            job_id: null,
            skipped_reason: "no new episodes",
          },
        }),
      ),
    ).toEqual({ kind: "skipped", ranAt: "2026-10-02T12:00:00Z", reason: "no new episodes" });
  });

  it("uses bare last_run_at when there is no summary", () => {
    expect(describeLastRun(sub({ last_run_at: "2026-10-01T00:00:00Z", last_job_id: "j2" }))).toEqual({
      kind: "ran",
      ranAt: "2026-10-01T00:00:00Z",
      newEpisodes: 0,
      jobId: "j2",
    });
  });
});

describe("draft persistence", () => {
  it("round-trips a draft", () => {
    const draft: SubscribeDraft = {
      ...completeDraft(),
      context: "ctx",
      cadence: "daily",
      voice: "two_host",
      highlightCount: 9,
      maxEpisodesPerRun: 12,
      notifyWhenEmpty: true,
      interview: { ...defaultDraft().interview, identity: "PM" },
    };
    expect(parseDraft(serializeDraft(draft))).toEqual(draft);
  });

  it("returns defaults for null, garbage, and non-objects", () => {
    expect(parseDraft(null)).toEqual(defaultDraft());
    expect(parseDraft("{not json")).toEqual(defaultDraft());
    expect(parseDraft("[1,2]")).toEqual(defaultDraft());
    expect(parseDraft("42")).toEqual(defaultDraft());
  });

  it("keeps good fields and defaults bad ones", () => {
    const parsed = parseDraft(
      JSON.stringify({
        email: 5,
        soul: "keep me",
        cadence: "monthly",
        highlightCount: 999,
        maxEpisodesPerRun: -4,
        voice: "robot",
        sources: [ACQUIRED, { kind: "rss" }, { kind: "bogus", x: 1 }, ACQUIRED, null],
        interview: { identity: "PM", style: 3 },
      }),
    );
    expect(parsed.email).toBe("");
    expect(parsed.soul).toBe("keep me");
    expect(parsed.cadence).toBe("weekly");
    expect(parsed.highlightCount).toBe(20);
    expect(parsed.maxEpisodesPerRun).toBe(1);
    expect(parsed.voice).toBe("single");
    expect(parsed.sources).toEqual([ACQUIRED]);
    expect(parsed.interview.identity).toBe("PM");
    expect(parsed.interview.style).toBe("");
  });
});
