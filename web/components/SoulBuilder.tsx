"use client";

import { useId, useRef, useState, type KeyboardEvent } from "react";

import { buildSoulFromInterview } from "@/lib/api-client";
import { describeApiError } from "@/lib/api-errors";
import { INTERVIEW_KEYS, MAX_SOUL_CHARS, type InterviewKey } from "@/lib/api-types";
import {
  INTERVIEW_QUESTIONS,
  cleanInterviewAnswers,
  interviewHasAnswers,
} from "@/lib/subscribe";

import {
  ERROR_TEXT_CLASS,
  FIELD_LABEL_CLASS,
  HELP_TEXT_CLASS,
  INPUT_CLASS,
  SECONDARY_BUTTON_CLASS,
  TEXTAREA_CLASS,
} from "./ui";

type Tab = "interview" | "paste";

const TABS: Array<{ id: Tab; label: string }> = [
  { id: "interview", label: "Answer six questions" },
  { id: "paste", label: "Paste a soul" },
];

/**
 * Build the "soul" (the reading lens): either answer the six interview
 * questions (POST /souls/interview writes the markdown) or paste a soul
 * directly. Either way the result lands in one editable textarea. Soul and
 * answers are controlled by the parent so the wizard can persist them.
 */
export function SoulBuilder({
  soul,
  onSoulChange,
  answers,
  onAnswersChange,
  baseUrl,
  token,
  onAuthError,
}: {
  soul: string;
  onSoulChange: (next: string) => void;
  answers: Record<InterviewKey, string>;
  onAnswersChange: (next: Record<InterviewKey, string>) => void;
  baseUrl: string;
  token: string;
  /** Called when the API rejects the key (401/403). */
  onAuthError?: () => void;
}) {
  const uid = useId();
  const [tab, setTab] = useState<Tab>(() =>
    soul.trim().length > 0 && !interviewHasAnswers(answers) ? "paste" : "interview",
  );
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [written, setWritten] = useState(false);
  const tabRefs = useRef<Record<Tab, HTMLButtonElement | null>>({ interview: null, paste: null });

  function selectTab(next: Tab, focus: boolean) {
    setTab(next);
    if (focus) tabRefs.current[next]?.focus();
  }

  function handleTabKey(e: KeyboardEvent<HTMLButtonElement>) {
    const index = TABS.findIndex((t) => t.id === tab);
    let nextIndex: number | null = null;
    if (e.key === "ArrowRight") nextIndex = (index + 1) % TABS.length;
    else if (e.key === "ArrowLeft") nextIndex = (index - 1 + TABS.length) % TABS.length;
    else if (e.key === "Home") nextIndex = 0;
    else if (e.key === "End") nextIndex = TABS.length - 1;
    if (nextIndex === null) return;
    e.preventDefault();
    selectTab(TABS[nextIndex].id, true);
  }

  async function handleInterview() {
    if (!interviewHasAnswers(answers)) {
      setError("Answer at least one question first.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const text = await buildSoulFromInterview(baseUrl, token, cleanInterviewAnswers(answers));
      onSoulChange(text);
      setWritten(true);
    } catch (err) {
      const described = describeApiError(err, "Could not write your soul. Try again.");
      if (described.auth) onAuthError?.();
      setError(described.message);
    } finally {
      setBusy(false);
    }
  }

  const soulId = `${uid}-soul`;
  const soulHelpId = `${uid}-soul-help`;
  const overLimit = soul.length > MAX_SOUL_CHARS;

  const soulEditor = (
    <div className="space-y-1">
      <label htmlFor={soulId} className={FIELD_LABEL_CLASS}>
        {tab === "interview" ? "Your soul (edit anything that is off)" : "Your soul"}
      </label>
      <textarea
        id={soulId}
        value={soul}
        onChange={(e) => {
          onSoulChange(e.target.value);
          setWritten(false);
        }}
        rows={tab === "interview" ? 14 : 16}
        placeholder="# Soul — your lens as markdown"
        aria-describedby={soulHelpId}
        aria-invalid={overLimit}
        className={TEXTAREA_CLASS}
      />
      <p id={soulHelpId} className={overLimit ? ERROR_TEXT_CLASS : HELP_TEXT_CLASS}>
        <span className="font-mono">{soul.length.toLocaleString("en-US")}</span> /{" "}
        <span className="font-mono">{MAX_SOUL_CHARS.toLocaleString("en-US")}</span> characters.
        {tab === "paste"
          ? " A soul is markdown that says what you care about, what to skip, and how high the bar is."
          : ""}
      </p>
    </div>
  );

  return (
    <div className="space-y-6">
      <div role="tablist" aria-label="How to write your soul" className="flex gap-6 border-b border-taupe">
        {TABS.map((t) => {
          const selected = tab === t.id;
          return (
            <button
              key={t.id}
              ref={(el) => {
                tabRefs.current[t.id] = el;
              }}
              type="button"
              role="tab"
              id={`${uid}-tab-${t.id}`}
              aria-selected={selected}
              aria-controls={`${uid}-panel-${t.id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => selectTab(t.id, false)}
              onKeyDown={handleTabKey}
              className={`-mb-px border-b-2 pb-2 font-sans text-xs uppercase tracking-wide focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-blue ${
                selected ? "border-navy-text font-semibold text-navy-text" : "border-transparent text-silver hover:text-navy-text"
              }`}
            >
              {t.label}
            </button>
          );
        })}
      </div>

      {tab === "interview" ? (
        <div
          role="tabpanel"
          id={`${uid}-panel-interview`}
          aria-labelledby={`${uid}-tab-interview`}
          className="space-y-6"
        >
          <p className="max-w-2xl font-serif text-[15px] leading-relaxed text-ink">
            Your answers become a short document, your soul, that Chorus reads every episode
            against. Plain words are fine; skip any question you cannot answer yet.
          </p>
          <div className="space-y-5">
            {INTERVIEW_KEYS.map((key, i) => {
              const q = INTERVIEW_QUESTIONS[key];
              const fieldId = `${uid}-q-${key}`;
              const hintId = `${fieldId}-hint`;
              const describedBy = q.hint ? hintId : undefined;
              const setAnswer = (value: string) => onAnswersChange({ ...answers, [key]: value });
              return (
                <div key={key}>
                  <label htmlFor={fieldId} className="mb-1 block font-serif text-[15px] text-navy-text">
                    <span className="mr-2 font-mono text-xs text-silver">{i + 1}</span>
                    {q.label}
                  </label>
                  {q.multiline ? (
                    <textarea
                      id={fieldId}
                      value={answers[key]}
                      onChange={(e) => setAnswer(e.target.value)}
                      rows={3}
                      aria-describedby={describedBy}
                      className={`${INPUT_CLASS} font-serif text-[15px]`}
                    />
                  ) : (
                    <input
                      id={fieldId}
                      type="text"
                      value={answers[key]}
                      onChange={(e) => setAnswer(e.target.value)}
                      aria-describedby={describedBy}
                      className={`${INPUT_CLASS} font-serif text-[15px]`}
                    />
                  )}
                  {q.hint ? (
                    <p id={hintId} className={`${HELP_TEXT_CLASS} mt-1`}>
                      {q.hint}
                    </p>
                  ) : null}
                </div>
              );
            })}
          </div>
          <div className="space-y-2">
            <button
              type="button"
              onClick={() => void handleInterview()}
              disabled={busy}
              className={SECONDARY_BUTTON_CLASS}
            >
              {busy ? "Writing…" : soul.trim() ? "Rewrite my soul" : "Write my soul"}
            </button>
            {soul.trim() ? (
              <p className={HELP_TEXT_CLASS}>Rewriting replaces the text below, including any edits.</p>
            ) : null}
            <div aria-live="polite" role={error ? "alert" : "status"}>
              {error ? <p className={ERROR_TEXT_CLASS}>{error}</p> : null}
              {written && !error ? (
                <p className={HELP_TEXT_CLASS}>Soul written. Read it through and edit anything that is off.</p>
              ) : null}
            </div>
          </div>
          {soul.trim() ? soulEditor : null}
        </div>
      ) : (
        <div
          role="tabpanel"
          id={`${uid}-panel-paste`}
          aria-labelledby={`${uid}-tab-paste`}
          className="space-y-3"
        >
          {soulEditor}
        </div>
      )}
    </div>
  );
}
