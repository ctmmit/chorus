"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";

import { SubscriptionRow } from "@/components/SubscriptionRow";
import { ERROR_TEXT_CLASS, HELP_TEXT_CLASS, LINK_CLASS, SECONDARY_BUTTON_CLASS } from "@/components/ui";
import { listSubscriptions } from "@/lib/api-client";
import { AUTH_REJECTED_MESSAGE, describeApiError } from "@/lib/api-errors";
import type { Subscription } from "@/lib/api-types";
import { FeedLink } from "@/components/FeedLink";
import { MOCK_MODE } from "@/lib/config";
import { getBaseUrl, getToken, setToken as persistToken } from "@/lib/storage";

type Load =
  | { state: "loading" }
  | { state: "error"; message: string }
  | { state: "ready"; subscriptions: Subscription[] };

/** /subscriptions: the signed-in person's subscriptions and what each can do. */
export function SubscriptionsView() {
  const [hydrated, setHydrated] = useState(false);
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [load, setLoad] = useState<Load>({ state: "loading" });
  const [reloadCount, setReloadCount] = useState(0);

  useEffect(() => {
    // One-time hydration from localStorage: unavailable during SSR.
    /* eslint-disable react-hooks/set-state-in-effect */
    setBaseUrl(getBaseUrl());
    setToken(getToken());
    setHydrated(true);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, []);

  const hasToken = token.trim().length > 0;

  useEffect(() => {
    if (!hydrated || !hasToken) return;
    let cancelled = false;
    listSubscriptions(baseUrl, token)
      .then((subscriptions) => {
        if (!cancelled) setLoad({ state: "ready", subscriptions });
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        const described = describeApiError(err, "Could not load your subscriptions.");
        if (described.auth) {
          persistToken("");
          setToken("");
        }
        setLoad({ state: "error", message: described.message });
      });
    return () => {
      cancelled = true;
    };
  }, [hydrated, hasToken, baseUrl, token, reloadCount]);

  const handleAuthError = useCallback(() => {
    persistToken("");
    setToken("");
    setLoad({ state: "error", message: AUTH_REJECTED_MESSAGE });
  }, []);

  function replaceSubscription(next: Subscription) {
    setLoad((current) =>
      current.state === "ready"
        ? {
            state: "ready",
            subscriptions: current.subscriptions.map((s) =>
              s.subscription_id === next.subscription_id ? next : s,
            ),
          }
        : current,
    );
  }

  function removeSubscription(id: string) {
    setLoad((current) =>
      current.state === "ready"
        ? { state: "ready", subscriptions: current.subscriptions.filter((s) => s.subscription_id !== id) }
        : current,
    );
  }

  if (!hydrated) return <p className="label-caps">Loading…</p>;

  const header = (
    <header className="space-y-2">
      <h1 className="font-serif text-2xl text-navy-text sm:text-[28px]">Subscriptions</h1>
      <p className="max-w-2xl font-serif text-[15px] leading-relaxed text-ink">
        Your digests: what each covers, when it runs next, and what the last run did.
      </p>
      {MOCK_MODE ? (
        <p className="label-caps text-blue">Mock mode — subscriptions live in memory on the dev server.</p>
      ) : null}
    </header>
  );

  if (!hasToken) {
    return (
      <div className="space-y-6">
        {header}
        <div aria-live="polite" className="space-y-3">
          {load.state === "error" ? (
            <p role="alert" className={ERROR_TEXT_CLASS}>
              {load.message}
            </p>
          ) : null}
          <p className="font-serif text-[15px] text-ink">Sign in to see your subscriptions.</p>
          <Link href="/subscribe" className={`${LINK_CLASS} font-sans text-sm`}>
            Subscribe or sign in
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-8">
      {header}
      <FeedLink baseUrl={baseUrl} token={token} />

      <div aria-live="polite">
        {load.state === "loading" ? <p className="label-caps">Loading your subscriptions…</p> : null}
        {load.state === "error" ? (
          <div className="space-y-3">
            <p role="alert" className={ERROR_TEXT_CLASS}>
              {load.message}
            </p>
            <button
              type="button"
              onClick={() => {
                setLoad({ state: "loading" });
                setReloadCount((n) => n + 1);
              }}
              className={SECONDARY_BUTTON_CLASS}
            >
              Try again
            </button>
          </div>
        ) : null}
      </div>

      {load.state === "ready" ? (
        load.subscriptions.length === 0 ? (
          <div className="space-y-3">
            <p className="font-serif text-[15px] text-ink">You have no subscriptions yet.</p>
            <Link href="/subscribe" className={`${LINK_CLASS} font-sans text-sm`}>
              Subscribe to your first shows
            </Link>
          </div>
        ) : (
          <>
            <ul className="divide-y divide-taupe border-y border-navy-text">
              {load.subscriptions.map((subscription) => (
                <SubscriptionRow
                  key={subscription.subscription_id}
                  subscription={subscription}
                  baseUrl={baseUrl}
                  token={token}
                  onUpdated={replaceSubscription}
                  onDeleted={removeSubscription}
                  onAuthError={handleAuthError}
                />
              ))}
            </ul>
            <p className={HELP_TEXT_CLASS}>
              <Link href="/subscribe" className={LINK_CLASS}>
                Add another subscription
              </Link>
            </p>
          </>
        )
      ) : null}
    </div>
  );
}
