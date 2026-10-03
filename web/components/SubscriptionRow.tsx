"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useId, useState } from "react";

import {
  deleteSubscription,
  runSubscription,
  updateSubscription,
} from "@/lib/api-client";
import { describeApiError } from "@/lib/api-errors";
import { MAX_CONTEXT_CHARS, type Source, type Subscription } from "@/lib/api-types";
import { addRecentJob } from "@/lib/storage";
import {
  CADENCE_LABELS,
  describeLastRun,
  formatDateTime,
  insecureFeedSources,
  interpretRun,
  skippedRunMessage,
  sourceKey,
  sourceTitle,
  subscriptionSources,
  summarizeSourceTitles,
} from "@/lib/subscribe";

import { SourcePicker } from "./SourcePicker";
import {
  ERROR_TEXT_CLASS,
  FIELD_LABEL_CLASS,
  HELP_TEXT_CLASS,
  LINK_CLASS,
  PRIMARY_BUTTON_CLASS,
  SECONDARY_BUTTON_CLASS,
  TEXTAREA_CLASS,
  TEXT_BUTTON_CLASS,
} from "./ui";

type Mode = "view" | "context" | "shows";

const SHOW_TITLE_LIMIT = 4;

function sameSources(a: Source[], b: Source[]): boolean {
  return a.length === b.length && a.every((s, i) => sourceKey(s) === sourceKey(b[i]));
}

/**
 * One subscription on /subscriptions: what it covers, when it runs next, what
 * the last run did, and the actions (pause or resume, edit context, edit
 * shows, run now, delete with confirmation).
 */
export function SubscriptionRow({
  subscription,
  baseUrl,
  token,
  onUpdated,
  onDeleted,
  onAuthError,
}: {
  subscription: Subscription;
  baseUrl: string;
  token: string;
  onUpdated: (next: Subscription) => void;
  onDeleted: (subscriptionId: string) => void;
  onAuthError: () => void;
}) {
  const uid = useId();
  const router = useRouter();
  const sources = subscriptionSources(subscription);
  const lastRun = describeLastRun(subscription);
  const insecure = insecureFeedSources(sources);

  const [mode, setMode] = useState<Mode>("view");
  const [contextText, setContextText] = useState(subscription.context);
  const [sourcesDraft, setSourcesDraft] = useState<Source[]>(sources);
  const [busy, setBusy] = useState<null | "toggle" | "context" | "shows" | "run" | "delete">(null);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState("");
  const [confirmingDelete, setConfirmingDelete] = useState(false);

  function fail(err: unknown, fallback: string) {
    const described = describeApiError(err, fallback);
    if (described.auth) onAuthError();
    setError(described.message);
  }

  async function patch(
    action: "toggle" | "context" | "shows",
    body: Parameters<typeof updateSubscription>[3],
    done: string,
  ) {
    setBusy(action);
    setError(null);
    try {
      const next = await updateSubscription(baseUrl, token, subscription.subscription_id, body);
      onUpdated(next);
      setStatus(done);
      setMode("view");
    } catch (err) {
      fail(err, "Could not save that change. Try again.");
    } finally {
      setBusy(null);
    }
  }

  async function handleRun() {
    setBusy("run");
    setError(null);
    try {
      const outcome = interpretRun(await runSubscription(baseUrl, token, subscription.subscription_id));
      if (outcome.kind === "skipped") {
        setStatus(skippedRunMessage(outcome.reason));
        setBusy(null);
        return;
      }
      addRecentJob({
        job_id: outcome.jobId,
        base_url: baseUrl,
        created_at: new Date().toISOString(),
        label: `run now · ${sources.length} ${sources.length === 1 ? "show" : "shows"}`,
      });
      router.push(`/jobs/${outcome.jobId}`);
    } catch (err) {
      fail(err, "Could not start the digest. Try again.");
      setBusy(null);
    }
  }

  async function handleDelete() {
    setBusy("delete");
    setError(null);
    try {
      await deleteSubscription(baseUrl, token, subscription.subscription_id);
      onDeleted(subscription.subscription_id);
    } catch (err) {
      fail(err, "Could not delete this subscription. Try again.");
      setBusy(null);
      setConfirmingDelete(false);
    }
  }

  const contextOver = contextText.length > MAX_CONTEXT_CHARS;
  const contextChanged = contextText !== subscription.context;
  const showsChanged = !sameSources(sourcesDraft, sources);
  const disabled = busy !== null;

  return (
    <li className="space-y-5 py-8">
      <div className="space-y-1">
        <h2 className="font-serif text-lg text-navy-text">{summarizeSourceTitles(sources, SHOW_TITLE_LIMIT)}</h2>
        <p className={HELP_TEXT_CLASS}>
          <span className="font-mono">{sources.length}</span> {sources.length === 1 ? "show" : "shows"}
          {" · "}
          {subscription.email}
        </p>
      </div>

      {insecure.length > 0 ? (
        <p role="note" className={`${ERROR_TEXT_CLASS} max-w-2xl`}>
          Chorus cannot read{" "}
          {insecure.map((s) => sourceTitle(s)).join(", ")}: the feed address starts with http://, and feeds
          must use https://. Replace it under Edit shows.
        </p>
      ) : null}

      <dl className="grid max-w-2xl grid-cols-[7rem_1fr] gap-x-6 gap-y-2 text-[15px]">
        <dt className="label-caps self-baseline">Status</dt>
        <dd className={`font-sans text-xs uppercase tracking-[0.12em] ${subscription.active ? "text-navy-text" : "text-silver"}`}>
          {subscription.active ? "Active" : "Paused"}
        </dd>

        <dt className="label-caps self-baseline">Schedule</dt>
        <dd className="font-serif text-ink">{CADENCE_LABELS[subscription.cadence]}</dd>

        <dt className="label-caps self-baseline">Next digest</dt>
        <dd className="font-mono text-sm text-navy-text">
          {subscription.active ? formatDateTime(subscription.next_run_at) : <span className="text-silver">Paused</span>}
        </dd>

        <dt className="label-caps self-baseline">Last digest</dt>
        <dd className="font-serif text-ink">
          {lastRun.kind === "none" ? (
            <span className="italic text-silver">No digests yet.</span>
          ) : lastRun.kind === "skipped" ? (
            <>
              <span className="font-mono text-sm">{formatDateTime(lastRun.ranAt)}</span>
              {" · Skipped: "}
              {lastRun.reason}
            </>
          ) : (
            <>
              <span className="font-mono text-sm">{formatDateTime(lastRun.ranAt)}</span>
              {" · "}
              <span className="font-mono text-sm">{lastRun.newEpisodes}</span> new{" "}
              {lastRun.newEpisodes === 1 ? "episode" : "episodes"}
              {lastRun.jobId ? (
                <>
                  {" · "}
                  <Link href={`/jobs/${lastRun.jobId}`} className={LINK_CLASS}>
                    View digest
                  </Link>
                </>
              ) : null}
            </>
          )}
        </dd>

        {subscription.context.trim() ? (
          <>
            <dt className="label-caps self-baseline">This week</dt>
            <dd className="font-serif text-sm italic text-silver">{subscription.context}</dd>
          </>
        ) : null}
      </dl>

      {/* Actions */}
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
        <button
          type="button"
          disabled={disabled}
          onClick={() =>
            void patch(
              "toggle",
              { active: !subscription.active },
              subscription.active ? "Subscription paused." : "Subscription resumed.",
            )
          }
          className={SECONDARY_BUTTON_CLASS}
        >
          {busy === "toggle" ? "Saving…" : subscription.active ? "Pause" : "Resume"}
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => void handleRun()}
          className={SECONDARY_BUTTON_CLASS}
        >
          {busy === "run" ? "Starting…" : "Run now"}
        </button>
        <button
          type="button"
          disabled={disabled}
          aria-expanded={mode === "context"}
          onClick={() => {
            setContextText(subscription.context);
            setMode(mode === "context" ? "view" : "context");
          }}
          className={TEXT_BUTTON_CLASS}
        >
          Edit context
        </button>
        <button
          type="button"
          disabled={disabled}
          aria-expanded={mode === "shows"}
          onClick={() => {
            setSourcesDraft(sources);
            setMode(mode === "shows" ? "view" : "shows");
          }}
          className={TEXT_BUTTON_CLASS}
        >
          Edit shows
        </button>
        {confirmingDelete ? (
          <span role="group" aria-label="Confirm delete" className="flex items-center gap-3">
            <span className="font-sans text-xs text-ink">Delete this subscription?</span>
            <button type="button" disabled={disabled} onClick={() => void handleDelete()} className={SECONDARY_BUTTON_CLASS}>
              {busy === "delete" ? "Deleting…" : "Yes, delete"}
            </button>
            <button type="button" disabled={disabled} onClick={() => setConfirmingDelete(false)} className={TEXT_BUTTON_CLASS}>
              Keep it
            </button>
          </span>
        ) : (
          <button
            type="button"
            disabled={disabled}
            onClick={() => setConfirmingDelete(true)}
            className={`${TEXT_BUTTON_CLASS} text-silver`}
          >
            Delete
          </button>
        )}
      </div>

      <div aria-live="polite">
        {error ? (
          <p role="alert" className={ERROR_TEXT_CLASS}>
            {error}
          </p>
        ) : status ? (
          <p role="status" className={HELP_TEXT_CLASS}>
            {status}
          </p>
        ) : null}
      </div>

      {/* Inline editors */}
      {mode === "context" ? (
        <div className="max-w-2xl space-y-3 border-t border-taupe pt-5">
          <label htmlFor={`${uid}-context`} className={FIELD_LABEL_CLASS}>
            What are you working on this week?
          </label>
          <textarea
            id={`${uid}-context`}
            value={contextText}
            onChange={(e) => setContextText(e.target.value)}
            rows={5}
            aria-invalid={contextOver}
            className={TEXTAREA_CLASS}
          />
          {contextOver ? (
            <p className={ERROR_TEXT_CLASS}>Over the {MAX_CONTEXT_CHARS.toLocaleString("en-US")} character limit.</p>
          ) : null}
          <div className="flex items-center gap-4">
            <button
              type="button"
              disabled={disabled || contextOver || !contextChanged}
              onClick={() => void patch("context", { context: contextText }, "Context saved.")}
              className={PRIMARY_BUTTON_CLASS}
            >
              {busy === "context" ? "Saving…" : "Save"}
            </button>
            <button type="button" disabled={disabled} onClick={() => setMode("view")} className={TEXT_BUTTON_CLASS}>
              Cancel
            </button>
          </div>
        </div>
      ) : null}

      {mode === "shows" ? (
        <div className="space-y-6 border-t border-taupe pt-5">
          <SourcePicker
            sources={sourcesDraft}
            onChange={setSourcesDraft}
            baseUrl={baseUrl}
            token={token}
            onAuthError={onAuthError}
          />
          <div className="space-y-2">
            <div className="flex items-center gap-4">
              <button
                type="button"
                disabled={disabled || sourcesDraft.length === 0 || !showsChanged}
                onClick={() => void patch("shows", { sources: sourcesDraft }, "Shows saved.")}
                className={PRIMARY_BUTTON_CLASS}
              >
                {busy === "shows" ? "Saving…" : "Save shows"}
              </button>
              <button type="button" disabled={disabled} onClick={() => setMode("view")} className={TEXT_BUTTON_CLASS}>
                Cancel
              </button>
            </div>
            {sourcesDraft.length === 0 ? (
              <p className={HELP_TEXT_CLASS}>Keep at least one show, or delete the subscription instead.</p>
            ) : null}
          </div>
        </div>
      ) : null}
    </li>
  );
}
