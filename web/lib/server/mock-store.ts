/**
 * In-memory subscription store for mock mode. State lives on `globalThis`
 * so it survives dev-server hot reloads and is shared by every route
 * handler module instance for the life of the server process; restarting
 * `next dev` resets it to the seed below. Never import from a Client
 * Component.
 */
import type {
  Source,
  Subscription,
  SubscriptionCreateRequest,
  SubscriptionUpdateRequest,
} from "@/lib/api-types";
import { applyRun, applyUpdate, nextRunAt, subscriptionFromRequest } from "@/lib/server/mock-subscribe";

const MOCK_OWNER = "demo@example.com";
const STORE_KEY = "__chorusMockSubscriptions";

interface StoreShape {
  subscriptions: Map<string, Subscription>;
  counter: number;
}

const SEED_SOUL = [
  "# Soul (interview)",
  "",
  "## Identity & Role",
  "Research director at a multi-family office; owns manager and idea selection.",
  "",
  "## Core Interests",
  "- Capital allocation",
  "- Private credit",
  "",
  "## Attention Triggers",
  "- A falsifiable claim with a number behind it",
  "",
  "## Anti-interests",
  "- Celebrity and who-said-what",
  "",
  "## Taste & Sensibility",
  "Empirical.",
  "",
  "## Curation Guidance",
  "Surface only segments that change a decision; refuse rather than pad.",
  "",
].join("\n");

const SEED_SOURCES: Source[] = [
  {
    kind: "rss",
    feed_url: "https://feeds.transistor.fm/acquired",
    title: "Acquired",
    artwork_url: "/api/mock/artwork?t=Acquired",
  },
  {
    kind: "rss",
    feed_url: "https://feeds.megaphone.fm/investlikethebest",
    title: "Invest Like the Best",
    artwork_url: "/api/mock/artwork?t=Invest%20Like%20the%20Best",
  },
  {
    kind: "rss",
    feed_url: "https://feeds.simplecast.com/capitalallocators",
    title: "Capital Allocators",
    artwork_url: "/api/mock/artwork?t=Capital%20Allocators",
  },
];

function seedStore(now: Date): StoreShape {
  const base = (id: string, sources: Source[], cadence: "weekly" | "daily"): Subscription =>
    subscriptionFromRequest(
      {
        email: MOCK_OWNER,
        soul: SEED_SOUL,
        context: "",
        sources,
        cadence,
        highlight_count: 4,
        profile: null,
        max_episodes_per_run: 5,
        notify_when_empty: false,
      },
      id,
      MOCK_OWNER,
      new Date(now.getTime() - 21 * 86_400_000),
    );

  const ran = base("sub_demo_1", SEED_SOURCES, "weekly");
  const lastRun = new Date(now.getTime() - 3 * 86_400_000);
  const active: Subscription = {
    ...ran,
    context: "Reading about private-credit marks and bank funding costs this week.",
    next_run_at: nextRunAt("weekly", now).toISOString(),
    last_job_id: "e94187e7f25e4383809d16378e309c9c",
    last_run_at: lastRun.toISOString(),
    last_run_summary: {
      ran_at: lastRun.toISOString(),
      new_episodes: 4,
      job_id: "e94187e7f25e4383809d16378e309c9c",
      skipped_reason: null,
    },
  };

  const pausedBase = base("sub_demo_2", [SEED_SOURCES[2]], "daily");
  const skippedAt = new Date(now.getTime() - 86_400_000);
  const paused: Subscription = {
    ...pausedBase,
    active: false,
    soul: SEED_SOUL,
    next_run_at: nextRunAt("daily", now).toISOString(),
    last_run_at: skippedAt.toISOString(),
    last_run_summary: {
      ran_at: skippedAt.toISOString(),
      new_episodes: 0,
      job_id: null,
      skipped_reason: "No new episodes since the last digest.",
    },
  };

  return {
    subscriptions: new Map([
      [active.subscription_id, active],
      [paused.subscription_id, paused],
    ]),
    counter: 2,
  };
}

function store(): StoreShape {
  const holder = globalThis as unknown as Record<string, StoreShape | undefined>;
  let existing = holder[STORE_KEY];
  if (!existing) {
    existing = seedStore(new Date());
    holder[STORE_KEY] = existing;
  }
  return existing;
}

export function listMockSubscriptions(): Subscription[] {
  return [...store().subscriptions.values()].sort((a, b) => b.created_at.localeCompare(a.created_at));
}

export function getMockSubscription(id: string): Subscription | null {
  return store().subscriptions.get(id) ?? null;
}

export function createMockSubscription(body: SubscriptionCreateRequest): Subscription {
  const s = store();
  s.counter += 1;
  const sub = subscriptionFromRequest(body, `sub_demo_${s.counter}`, MOCK_OWNER, new Date());
  s.subscriptions.set(sub.subscription_id, sub);
  return sub;
}

export function updateMockSubscription(id: string, patch: SubscriptionUpdateRequest): Subscription | null {
  const s = store();
  const current = s.subscriptions.get(id);
  if (!current) return null;
  const next = applyUpdate(current, patch, new Date());
  s.subscriptions.set(id, next);
  return next;
}

export function deleteMockSubscription(id: string): boolean {
  return store().subscriptions.delete(id);
}

export function recordMockRun(id: string, jobId: string, newEpisodes: number): Subscription | null {
  const s = store();
  const current = s.subscriptions.get(id);
  if (!current) return null;
  const next = applyRun(current, jobId, newEpisodes, new Date());
  s.subscriptions.set(id, next);
  return next;
}
