"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";

import { ScheduleStep } from "@/components/ScheduleStep";
import { SignInStep } from "@/components/SignInStep";
import { SoulBuilder } from "@/components/SoulBuilder";
import { SourcePicker } from "@/components/SourcePicker";
import { StepIndicator } from "@/components/StepIndicator";
import {
  ERROR_TEXT_CLASS,
  FIELD_LABEL_CLASS,
  HELP_TEXT_CLASS,
  LINK_CLASS,
  PRIMARY_BUTTON_CLASS,
  SECONDARY_BUTTON_CLASS,
  TEXTAREA_CLASS,
} from "@/components/ui";
import { runSubscription } from "@/lib/api-client";
import { AUTH_REJECTED_MESSAGE, describeApiError } from "@/lib/api-errors";
import { MAX_CONTEXT_CHARS, type Subscription } from "@/lib/api-types";
import { MOCK_MODE } from "@/lib/config";
import {
  CADENCE_LABELS,
  clampStep,
  defaultDraft,
  formatDateTime,
  fragmentForStep,
  furthestReachableStep,
  interpretRun,
  skippedRunMessage,
  parseDraft,
  parseFragmentStep,
  parseFragmentToken,
  serializeDraft,
  summarizeSourceTitles,
  validateStep,
  WIZARD_STEPS,
  type SubscribeDraft,
  type WizardStep,
} from "@/lib/subscribe";
import {
  addRecentJob,
  clearSubscribeDraft,
  getBaseUrl,
  getSubscribeDraftRaw,
  getToken,
  setBaseUrl as persistBaseUrl,
  setSubscribeDraftRaw,
  setToken as persistToken,
} from "@/lib/storage";

const STEP_INTRO: Record<WizardStep, string> = {
  1: "Chorus knows you by a key, not a password.",
  2: "Choose the podcasts Chorus should listen to for you.",
  3: "Tell Chorus what matters to you, so it can skip the rest.",
  4: "Set the rhythm, check what the first digest would cover, then subscribe.",
};

/** Rewrite the address-bar fragment without adding a history entry. */
function writeFragment(fragment: string): void {
  if (typeof window === "undefined") return;
  const { pathname, search } = window.location;
  window.history.replaceState(window.history.state, "", `${pathname}${search}${fragment}`);
}

/**
 * The human subscribe flow: sign in, choose shows, describe your lens, pick a
 * schedule and preview, subscribe. The current step lives in the URL fragment
 * (`#step=N`) and everything entered is kept in localStorage, so a reload or
 * a return visit resumes where the person left off.
 */
export function SubscribeWizard() {
  const router = useRouter();

  const [hydrated, setHydrated] = useState(false);
  const [baseUrl, setBaseUrlState] = useState("");
  const [token, setTokenState] = useState("");
  const [signedInByLink, setSignedInByLink] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [draft, setDraft] = useState<SubscribeDraft>(defaultDraft);
  const [step, setStep] = useState<WizardStep>(1);
  const [created, setCreated] = useState<Subscription | null>(null);
  const [runBusy, setRunBusy] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);
  const [runNote, setRunNote] = useState<string | null>(null);

  const headingRef = useRef<HTMLHeadingElement>(null);
  const focusHeadingNext = useRef(false);

  const hasToken = token.trim().length > 0;
  const furthest = furthestReachableStep(draft, hasToken);

  useEffect(() => {
    // One-time hydration from the URL fragment and localStorage: neither is
    // available during SSR, so there is no way to seed this state earlier.
    /* eslint-disable react-hooks/set-state-in-effect */
    const hash = window.location.hash;
    let activeToken = getToken();
    const fragmentToken = parseFragmentToken(hash);
    if (fragmentToken) {
      // The sign-in link from the key email: keep the key, then scrub it from
      // the address bar so it never lands in history, screenshots, or a share.
      persistToken(fragmentToken);
      activeToken = fragmentToken;
    }
    const loaded = parseDraft(getSubscribeDraftRaw());
    const tokenPresent = activeToken.trim().length > 0;
    const requested = parseFragmentStep(hash) ?? furthestReachableStep(loaded, tokenPresent);
    const initial = clampStep(requested, loaded, tokenPresent);
    // Replaces the whole fragment: the token is gone, `#step=N` records where we are.
    writeFragment(fragmentForStep(initial));

    setBaseUrlState(getBaseUrl());
    setTokenState(activeToken);
    setSignedInByLink(fragmentToken !== null);
    setDraft(loaded);
    setStep(initial);
    setHydrated(true);
    /* eslint-enable react-hooks/set-state-in-effect */
  }, []);

  // Keep what the person has entered, except after a successful subscribe.
  useEffect(() => {
    if (!hydrated || created) return;
    setSubscribeDraftRaw(serializeDraft(draft));
  }, [draft, hydrated, created]);

  // Move focus to the step heading after a step change the person asked for.
  useEffect(() => {
    if (focusHeadingNext.current) {
      focusHeadingNext.current = false;
      headingRef.current?.focus();
    }
  }, [step]);

  const patch = useCallback((partial: Partial<SubscribeDraft>) => {
    setDraft((d) => ({ ...d, ...partial }));
  }, []);

  function goTo(target: number, tokenPresent: boolean = hasToken) {
    const next = clampStep(target, draft, tokenPresent);
    focusHeadingNext.current = true;
    setStep(next);
    writeFragment(fragmentForStep(next));
  }

  const handleAuthError = useCallback(() => {
    persistToken("");
    setTokenState("");
    setSignedInByLink(false);
    setNotice(AUTH_REJECTED_MESSAGE);
    focusHeadingNext.current = true;
    setStep(1);
    writeFragment(fragmentForStep(1));
  }, []);

  function acceptToken(next: string) {
    persistToken(next);
    setTokenState(next);
    setSignedInByLink(false);
    setNotice(null);
    goTo(furthestReachableStep(draft, true), true);
  }

  function signOut() {
    persistToken("");
    setTokenState("");
    setSignedInByLink(false);
    setNotice(null);
    focusHeadingNext.current = true;
    setStep(1);
    writeFragment(fragmentForStep(1));
  }

  function handleCreated(subscription: Subscription) {
    clearSubscribeDraft();
    setCreated(subscription);
    writeFragment("");
    window.scrollTo({ top: 0 });
  }

  async function handleRunNow() {
    if (!created) return;
    setRunBusy(true);
    setRunError(null);
    setRunNote(null);
    try {
      const outcome = interpretRun(await runSubscription(baseUrl, token, created.subscription_id));
      if (outcome.kind === "skipped") {
        setRunNote(skippedRunMessage(outcome.reason));
        setRunBusy(false);
        return;
      }
      const count = created.sources?.length ?? 0;
      addRecentJob({
        job_id: outcome.jobId,
        base_url: baseUrl,
        created_at: new Date().toISOString(),
        label: `first digest · ${count} ${count === 1 ? "show" : "shows"}`,
      });
      router.push(`/jobs/${outcome.jobId}`);
    } catch (err) {
      setRunError(describeApiError(err, "Could not start the digest. Try again from your subscriptions.").message);
      setRunBusy(false);
    }
  }

  if (!hydrated) {
    return <p className="label-caps">Loading…</p>;
  }

  // --- Confirmation ---------------------------------------------------------
  if (created) {
    const sources = created.sources ?? [];
    return (
      <div className="space-y-8">
        <header className="space-y-2">
          <h1 className="font-serif text-2xl text-navy-text sm:text-[28px]" ref={headingRef} tabIndex={-1}>
            You are subscribed.
          </h1>
          <p className="max-w-2xl font-serif text-[15px] leading-relaxed text-ink">
            {created.cadence === "weekly" ? "A weekly" : "A daily"} digest of{" "}
            <span className="font-mono">{sources.length}</span> {sources.length === 1 ? "show" : "shows"}{" "}
            will arrive at {created.email}.
          </p>
        </header>

        <dl className="grid max-w-xl grid-cols-[8rem_1fr] gap-x-6 gap-y-3 font-serif text-[15px]">
          <dt className="label-caps self-baseline">Next digest</dt>
          <dd className="font-mono text-sm text-navy-text">{formatDateTime(created.next_run_at)}</dd>
          <dt className="label-caps self-baseline">Shows</dt>
          <dd className="text-ink">{summarizeSourceTitles(sources, 4)}</dd>
          <dt className="label-caps self-baseline">Schedule</dt>
          <dd className="text-ink">{CADENCE_LABELS[created.cadence]}</dd>
        </dl>

        <div className="space-y-3">
          <div className="flex flex-wrap items-center gap-5">
            <button
              type="button"
              onClick={() => void handleRunNow()}
              disabled={runBusy}
              className={PRIMARY_BUTTON_CLASS}
            >
              {runBusy ? "Starting…" : "Run first digest now"}
            </button>
            <Link href="/subscriptions" className={`${LINK_CLASS} font-sans text-sm`}>
              View your subscriptions
            </Link>
          </div>
          <p className={HELP_TEXT_CLASS}>
            Running now builds a digest from the latest episodes immediately, so you do not have to wait for the
            schedule.
          </p>
          <div aria-live="polite">
            {runError ? (
              <p role="alert" className={ERROR_TEXT_CLASS}>
                {runError}
              </p>
            ) : runNote ? (
              <p role="status" className="font-serif text-[15px] text-ink">
                {runNote}
              </p>
            ) : null}
          </div>
        </div>
      </div>
    );
  }

  // --- Wizard ----------------------------------------------------------------
  const stepProblems = validateStep(step, draft, hasToken);
  const stepMeta = WIZARD_STEPS.find((s) => s.id === step) ?? WIZARD_STEPS[0];
  const contextOver = draft.context.length > MAX_CONTEXT_CHARS;

  return (
    <div className="space-y-10">
      <header className="space-y-2">
        <h1 className="font-serif text-2xl text-navy-text sm:text-[28px]">Subscribe</h1>
        <p className="max-w-2xl font-serif text-[15px] leading-relaxed text-ink">
          Get a podcast digest in your inbox, cut to what matters to you. Four short steps.
        </p>
        {MOCK_MODE ? (
          <p className="label-caps text-blue">Mock mode — nothing is sent; data lives in memory on the dev server.</p>
        ) : null}
      </header>

      <StepIndicator current={step} furthest={furthest} onSelect={(s) => goTo(s)} />

      <section aria-labelledby="wizard-step-heading" className="space-y-8 border-t border-navy-text pt-6">
        <div className="space-y-1">
          <h2
            id="wizard-step-heading"
            ref={headingRef}
            tabIndex={-1}
            className="font-serif text-xl text-navy-text outline-none"
          >
            <span className="mr-3 font-mono text-sm text-silver">{String(step).padStart(2, "0")}</span>
            {stepMeta.label}
          </h2>
          <p className={HELP_TEXT_CLASS}>{STEP_INTRO[step]}</p>
        </div>

        {step === 1 ? (
          <SignInStep
            baseUrl={baseUrl}
            onBaseUrlChange={(next) => {
              setBaseUrlState(next);
              persistBaseUrl(next);
            }}
            token={token}
            email={draft.email}
            onEmailChange={(email) => patch({ email })}
            notice={notice}
            signedInByLink={signedInByLink}
            onToken={acceptToken}
            onSignOut={signOut}
            onContinue={() => goTo(furthestReachableStep(draft, true))}
          />
        ) : null}

        {step === 2 ? (
          <SourcePicker
            sources={draft.sources}
            onChange={(sources) => patch({ sources })}
            baseUrl={baseUrl}
            token={token}
            onAuthError={handleAuthError}
          />
        ) : null}

        {step === 3 ? (
          <div className="space-y-10">
            <SoulBuilder
              soul={draft.soul}
              onSoulChange={(soul) => patch({ soul })}
              answers={draft.interview}
              onAnswersChange={(interview) => patch({ interview })}
              baseUrl={baseUrl}
              token={token}
              onAuthError={handleAuthError}
            />
            <div className="space-y-1 border-t border-taupe pt-6">
              <label htmlFor="wizard-context" className={FIELD_LABEL_CLASS}>
                What are you working on this week?
              </label>
              <textarea
                id="wizard-context"
                value={draft.context}
                onChange={(e) => patch({ context: e.target.value })}
                rows={4}
                placeholder="A deal you are sizing up, a question you are chasing, a meeting you are preparing for."
                aria-describedby="wizard-context-help"
                aria-invalid={contextOver}
                className={TEXTAREA_CLASS}
              />
              <p id="wizard-context-help" className={contextOver ? ERROR_TEXT_CLASS : HELP_TEXT_CLASS}>
                Optional. It tilts this digest toward your current work. You can update it any time from your
                subscriptions.
              </p>
            </div>
          </div>
        ) : null}

        {step === 4 ? (
          <ScheduleStep
            draft={draft}
            onPatch={patch}
            baseUrl={baseUrl}
            token={token}
            onAuthError={handleAuthError}
            onBack={() => goTo(3)}
            onCreated={handleCreated}
          />
        ) : null}

        {step === 2 || step === 3 ? (
          <div className="space-y-2 border-t border-taupe pt-6">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <button type="button" onClick={() => goTo(step - 1)} className={SECONDARY_BUTTON_CLASS}>
                Back
              </button>
              <button
                type="button"
                onClick={() => goTo(step + 1)}
                disabled={stepProblems.length > 0}
                aria-describedby={stepProblems.length > 0 ? "wizard-next-help" : undefined}
                className={PRIMARY_BUTTON_CLASS}
              >
                Next
              </button>
            </div>
            {stepProblems.length > 0 ? (
              <p id="wizard-next-help" className={HELP_TEXT_CLASS}>
                {stepProblems.join(" ")}
              </p>
            ) : null}
          </div>
        ) : null}
      </section>
    </div>
  );
}
