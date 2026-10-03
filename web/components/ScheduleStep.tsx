"use client";

import { useId, useMemo, useState } from "react";

import { createSubscription, previewSubscription } from "@/lib/api-client";
import { describeApiError } from "@/lib/api-errors";
import {
  MAX_EPISODES_PER_RUN,
  MAX_HIGHLIGHTS,
  MIN_EPISODES_PER_RUN,
  MIN_HIGHLIGHTS,
  type PreviewResponse,
  type Subscription,
} from "@/lib/api-types";
import {
  CADENCE_LABELS,
  buildSubscriptionRequest,
  formatDate,
  groupPreviewBySource,
  isValidEmail,
  lookbackDaysFor,
  previewErrorLabel,
  sourceKey,
  validateStep,
  type SubscribeDraft,
  type VoiceMode,
} from "@/lib/subscribe";

import { NumberField } from "./NumberField";
import {
  ERROR_TEXT_CLASS,
  FIELD_LABEL_CLASS,
  HELP_TEXT_CLASS,
  INPUT_CLASS,
  PRIMARY_BUTTON_CLASS,
  SECONDARY_BUTTON_CLASS,
} from "./ui";

interface PreviewState {
  /** Which inputs this result was computed for (see previewKey). */
  key: string;
  data: PreviewResponse;
}

const VOICE_OPTIONS: Array<{ id: VoiceMode; label: string; detail: string }> = [
  { id: "single", label: "Single voice", detail: "One host reads your digest." },
  {
    id: "two_host",
    label: "Two hosts",
    detail: "A host and a skeptical cohost who presses for the number, the counterexample, the mechanism.",
  },
];

/** Everything that changes what a preview would show. */
function previewKey(draft: SubscribeDraft): string {
  return JSON.stringify([
    draft.sources.map(sourceKey),
    draft.cadence,
    draft.maxEpisodesPerRun,
  ]);
}

/**
 * Step 4: cadence, depth, voice, delivery address, a preview of what the
 * first digest would cover, and the Subscribe action.
 */
export function ScheduleStep({
  draft,
  onPatch,
  baseUrl,
  token,
  onAuthError,
  onBack,
  onCreated,
}: {
  draft: SubscribeDraft;
  onPatch: (partial: Partial<SubscribeDraft>) => void;
  baseUrl: string;
  token: string;
  onAuthError: () => void;
  onBack: () => void;
  onCreated: (subscription: Subscription) => void;
}) {
  const uid = useId();
  const [preview, setPreview] = useState<PreviewState | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [submitBusy, setSubmitBusy] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  const key = useMemo(() => previewKey(draft), [draft]);
  const previewStale = preview !== null && preview.key !== key;
  const groups = useMemo(() => (preview ? groupPreviewBySource(preview.data.episodes) : []), [preview]);
  const lookback = lookbackDaysFor(draft.cadence);

  const problems = validateStep(4, draft, true);
  const emailProblem = draft.email.trim() !== "" && !isValidEmail(draft.email);

  async function handlePreview() {
    setPreviewBusy(true);
    setPreviewError(null);
    const requestedKey = key;
    try {
      const data = await previewSubscription(baseUrl, token, {
        sources: draft.sources,
        lookback_days: lookback,
        max_episodes_per_run: draft.maxEpisodesPerRun,
      });
      setPreview({ key: requestedKey, data });
    } catch (err) {
      const described = describeApiError(err, "Could not build a preview. Try again.");
      if (described.auth) onAuthError();
      setPreviewError(described.message);
    } finally {
      setPreviewBusy(false);
    }
  }

  async function handleSubscribe() {
    if (problems.length > 0) return;
    setSubmitBusy(true);
    setSubmitError(null);
    try {
      const subscription = await createSubscription(baseUrl, token, buildSubscriptionRequest(draft));
      onCreated(subscription);
    } catch (err) {
      const described = describeApiError(err, "Could not create your subscription. Try again.");
      if (described.auth) onAuthError();
      setSubmitError(described.message);
    } finally {
      setSubmitBusy(false);
    }
  }

  const emailId = `${uid}-email`;
  const emailHelpId = `${uid}-email-help`;
  const episodeCount = preview?.data.episodes.length ?? 0;

  return (
    <div className="space-y-10">
      <div className="grid gap-8 sm:grid-cols-2">
        <fieldset className="space-y-2">
          <legend className="label-caps mb-1">How often</legend>
          {(Object.keys(CADENCE_LABELS) as Array<keyof typeof CADENCE_LABELS>).map((cadence) => (
            <label key={cadence} className="flex items-center gap-2 font-serif text-[15px] text-ink">
              <input
                type="radio"
                name={`${uid}-cadence`}
                value={cadence}
                checked={draft.cadence === cadence}
                onChange={() => onPatch({ cadence })}
                className="accent-navy-text"
              />
              {CADENCE_LABELS[cadence]}
            </label>
          ))}
        </fieldset>

        <fieldset className="space-y-2">
          <legend className="label-caps mb-1">Voice</legend>
          {VOICE_OPTIONS.map((option) => (
            <label key={option.id} className="flex items-start gap-2 font-serif text-[15px] text-ink">
              <input
                type="radio"
                name={`${uid}-voice`}
                value={option.id}
                checked={draft.voice === option.id}
                onChange={() => onPatch({ voice: option.id })}
                className="mt-1.5 accent-navy-text"
              />
              <span>
                {option.label}
                <span className={`${HELP_TEXT_CLASS} block`}>{option.detail}</span>
              </span>
            </label>
          ))}
        </fieldset>
      </div>

      <div className="grid gap-8 sm:grid-cols-2">
        <NumberField
          label="Highlights per episode"
          value={draft.highlightCount}
          min={MIN_HIGHLIGHTS}
          max={MAX_HIGHLIGHTS}
          onChange={(highlightCount) => onPatch({ highlightCount })}
        />
        <NumberField
          label="Episodes per digest"
          hint={`${MIN_EPISODES_PER_RUN} to ${MAX_EPISODES_PER_RUN}. The newest unseen episodes come first.`}
          value={draft.maxEpisodesPerRun}
          min={MIN_EPISODES_PER_RUN}
          max={MAX_EPISODES_PER_RUN}
          onChange={(maxEpisodesPerRun) => onPatch({ maxEpisodesPerRun })}
        />
      </div>

      <div>
        <label htmlFor={emailId} className={FIELD_LABEL_CLASS}>
          Send digests to
        </label>
        <input
          id={emailId}
          type="email"
          autoComplete="email"
          value={draft.email}
          onChange={(e) => onPatch({ email: e.target.value })}
          placeholder="you@example.com"
          aria-describedby={emailHelpId}
          aria-invalid={emailProblem}
          className={`${INPUT_CLASS} sm:max-w-md`}
        />
        <p id={emailHelpId} className={`${emailProblem ? ERROR_TEXT_CLASS : HELP_TEXT_CLASS} mt-1`}>
          {emailProblem ? "That does not look like an email address." : "Your digest arrives here as an email with the audio."}
        </p>
      </div>

      <label className="flex items-start gap-2 font-serif text-[15px] text-ink">
        <input
          type="checkbox"
          checked={draft.notifyWhenEmpty}
          onChange={(e) => onPatch({ notifyWhenEmpty: e.target.checked })}
          className="mt-1.5 accent-navy-text"
        />
        <span>
          Email me even when nothing is new
          <span className={`${HELP_TEXT_CLASS} block`}>
            Otherwise Chorus stays quiet on weeks your shows have not published.
          </span>
        </span>
      </label>

      {/* Preview */}
      <section aria-labelledby={`${uid}-preview-h`} className="space-y-3 border-t border-navy-text pt-6">
        <div className="flex flex-wrap items-baseline justify-between gap-3">
          <h3 id={`${uid}-preview-h`} className="font-serif text-lg text-navy-text">
            What the first digest would cover
          </h3>
          <button
            type="button"
            onClick={() => void handlePreview()}
            disabled={previewBusy || draft.sources.length === 0}
            className={SECONDARY_BUTTON_CLASS}
          >
            {previewBusy ? "Checking feeds…" : preview ? "Preview again" : "Preview"}
          </button>
        </div>

        <div aria-live="polite" className="space-y-4">
          {previewBusy ? (
            <p className={HELP_TEXT_CLASS}>
              Reading <span className="font-mono">{draft.sources.length}</span>{" "}
              {draft.sources.length === 1 ? "feed" : "feeds"}…
            </p>
          ) : null}
          {previewError ? (
            <p role="alert" className={ERROR_TEXT_CLASS}>
              {previewError}
            </p>
          ) : null}
          {preview && !previewBusy ? (
            <div className="space-y-5">
              {previewStale ? (
                <p className={HELP_TEXT_CLASS}>You changed a setting since this preview. Preview again to refresh it.</p>
              ) : null}
              <p className="font-serif text-[15px] text-ink">
                {episodeCount === 0 ? (
                  <>
                    Nothing published in the last <span className="font-mono">{lookback}</span>{" "}
                    {lookback === 1 ? "day" : "days"}. Your first digest would wait for the next episode.
                  </>
                ) : (
                  <>
                    <span className="font-mono">{episodeCount}</span>{" "}
                    {episodeCount === 1 ? "episode" : "episodes"} from{" "}
                    <span className="font-mono">{groups.length}</span>{" "}
                    {groups.length === 1 ? "show" : "shows"} published in the last{" "}
                    <span className="font-mono">{lookback}</span> {lookback === 1 ? "day" : "days"}.
                  </>
                )}
              </p>
              {groups.map((group) => (
                <div key={group.source_title}>
                  <p className="font-sans text-xs font-semibold uppercase tracking-wide text-navy-text">
                    {group.source_title}
                  </p>
                  <ul className="mt-1 space-y-1">
                    {group.episodes.map((ep, i) => (
                      <li key={`${ep.title}-${i}`} className="flex items-baseline gap-3 font-serif text-sm text-ink">
                        <span className="w-[5.5rem] shrink-0 font-mono text-xs text-silver">
                          {formatDate(ep.published_at)}
                        </span>
                        <span>{ep.title}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
              {preview.data.errors.length > 0 ? (
                <div className="space-y-1">
                  <p className="label-caps">Could not read</p>
                  <ul className="space-y-1">
                    {preview.data.errors.map((e, i) => (
                      <li key={i} className="font-sans text-xs text-ink">
                        {previewErrorLabel(e)}
                        {": "}
                        <span className="text-red">{e.reason}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ) : null}
            </div>
          ) : null}
          {!preview && !previewBusy && !previewError ? (
            <p className={HELP_TEXT_CLASS}>
              Optional. See which recent episodes your {draft.cadence === "daily" ? "daily" : "weekly"}{" "}
              digest would pick up before you subscribe.
            </p>
          ) : null}
        </div>
      </section>

      {/* Actions */}
      <div className="space-y-3 border-t border-taupe pt-6">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <button type="button" onClick={onBack} className={SECONDARY_BUTTON_CLASS}>
            Back
          </button>
          <button
            type="button"
            onClick={() => void handleSubscribe()}
            disabled={submitBusy || problems.length > 0}
            aria-describedby={problems.length > 0 ? `${uid}-problems` : undefined}
            className={PRIMARY_BUTTON_CLASS}
          >
            {submitBusy ? "Subscribing…" : "Subscribe"}
          </button>
        </div>
        {problems.length > 0 ? (
          <p id={`${uid}-problems`} className={HELP_TEXT_CLASS}>
            {problems.join(" ")}
          </p>
        ) : null}
        <div aria-live="polite">
          {submitError ? (
            <p role="alert" className={ERROR_TEXT_CLASS}>
              {submitError}
            </p>
          ) : null}
        </div>
      </div>
    </div>
  );
}
